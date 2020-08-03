import numpy as np
import matplotlib.pyplot as plt
import matplotlib
from scipy.sparse import csr_matrix, identity, kron
from scipy.sparse.linalg import eigs, eigsh
import itertools
from scipy.linalg import block_diag, eig, expm, eigh
from scipy.sparse import save_npz, load_npz, csr_matrix, csc_matrix
import yaml
import copy
import warnings
import os
import time

from .eigenstates import AnharmonicDisplaced

class OpenPolymer:

    def __init__(self,site_energies,site_couplings,dipoles):
        """This initializes an object with an arbitrary number of site
energies and couplings

site_energies - list of excitation energies of individual sites

site_couplings - list of energetic couplings between singly-excited electronic 
states in the site basis, for example [J12,J13,...,J1N,J23,...,J2N,...]
"""
        self.num_sites = len(site_energies)
        self.energies = site_energies
        self.couplings = site_couplings
        self.dipoles = dipoles
        self.pols = ['x','y','z']

        self.up = np.zeros((2,2))
        self.up[1,0] = 1

        self.down = np.zeros((2,2))
        self.down[0,1] = 1

        self.ii = np.eye(2)

        self.sz = np.array([[0,0],[0,1]])

        self.occupied = np.zeros((2,2))
        self.occupied[1,1] = 1

        self.empty = np.zeros((2,2))
        self.empty[0,0] = 1

        self.set_up_list()
        self.set_down_list()
        self.set_occupied_list()
        self.set_empty_list()
        self.set_exchange_list()
        self.set_sz_list()

        self.make_mu_dict_site_basis()
        self.make_mu_site_basis('x')
        self.make_mu_up_site_basis('x')

        self.set_electronic_hamiltonian()

        self.set_electronic_total_occupation_number()

        self.set_manifold_eigensystems()
        self.set_electronic_eigensystem()

        # Values that need to be set
        self.optical_dephasing_gamma = .1
        self.optical_decoherence_gamma = 0
        self.site_to_site_dephasing_gamma = 0
        self.site_to_site_decoherence_gamma = 0
        self.exciton_decoherence_gamma = 0
        self.exciton_exciton_dephasing_gamma = .01
        self.kT = 1

    def make_electronic_hamiltonian(self):
        ham = self.energies[0] * self.occupied_list[0]
        for i in range(1,self.num_sites):
            ham += self.energies[i] * self.occupied_list[i]

        for i in range(len(self.exchange_list)):
            ham += self.couplings[i] * self.exchange_list[i]
            ham += np.conjugate(self.couplings[i]) * self.exchange_list[i].T

        return ham

    def set_electronic_hamiltonian(self):
        self.electronic_hamiltonian = self.make_electronic_hamiltonian()

    def electronic_vector_of_ones_kron(self,position,item):
        N = 2
        n = self.num_sites
        ones_list = [np.ones(N) for i in range(n-1)]
        ones_list.insert(position,item)
        vec = ones_list.pop(0)
        for next_item in ones_list:
            vec = np.kron(vec,next_item)
        return vec

    def set_electronic_total_occupation_number(self):
        N = 2
        n = self.num_sites
        single_mode_occ = np.arange(N)
        occ_num = self.electronic_vector_of_ones_kron(0,single_mode_occ)
        for i in range(1,n):
            occ_num += self.electronic_vector_of_ones_kron(i,single_mode_occ)
        self.electronic_total_occupation_number = occ_num

    def electronic_manifold_mask(self,manifold_num):
        """Creates a boolean mask to describe which states obey the truncation
           size collectively
"""
        manifold_inds = np.where(self.electronic_total_occupation_number == manifold_num)[0]
        return manifold_inds

    def electronic_subspace_mask(self,min_occ_num,max_occ_num):
        """Creates a boolean mask to describe which states obey the range of 
            electronic occupation collectively
"""
        manifold_inds = np.where((self.electronic_total_occupation_number >= min_occ_num) &
                                 (self.electronic_total_occupation_number <= max_occ_num))[0]
        return manifold_inds

    def extract_coherence(self,O,manifold1,manifold2):
        """Returns result of projecting the Operator O onto manifold1
            on the left and manifold2 on the right
"""
        manifold1_inds = self.electronic_manifold_mask(manifold1)
        manifold2_inds = self.electronic_manifold_mask(manifold2)
        O = O[manifold1_inds,:]
        O = O[:,manifold2_inds]
        return O
    
    def extract_manifold(self,O,manifold_num):
        """Projects operator into the given electronic excitation manifold
"""
        return self.extract_coherence(O,manifold_num,manifold_num)

    def coherence_to_full(self,O,manifold1,manifold2):
        """Creates an array of zeros of the size of the full Hilbert space,
            and fills the correct entries with the operator O existing in
            a particular optical coherence between manifolds
"""
        Ofull = np.zeros(self.electronic_hamiltonian.shape,dtype=O.dtype)
        manifold1_inds = self.electronic_manifold_mask(manifold1)
        manifold2_inds = self.electronic_manifold_mask(manifold2)
        for i in range(manifold2_inds.size):
            ind = manifold2_inds[i]
            Ofull[manifold1_inds,ind] = O[:,i]
        return Ofull
    
    def manifold_to_full(self,O,manifold_num):
        """Creates an array of zeros of the size of the full Hilbert space,
            and fills the correct entries with the operator O existing in
            a single optical manifold
"""
        return self.coherence_to_full(O,manifold_num,manifold_num)

    def extract_electronic_subspace(self,O,min_occ_num,max_occ_num):
        """Projects operator into the given electronic excitation manifold
"""
        manifold_inds = self.electronic_subspace_mask(min_occ_num,max_occ_num)
        O = O[manifold_inds,:]
        O = O[:,manifold_inds]
        return O

    def electronic_identity_kron(self,element_list):
        """Takes in a list of tuples (element, position)
"""
        num_identities = self.num_sites - len(element_list)
        if num_identities < 0:
            raise ValueError('Too many elements for Hilbert space')

        matrix_list = [self.ii for j in range(self.num_sites)]

        for el, pos in element_list:
            matrix_list[pos] = el
        return self.recursive_kron(matrix_list)

    def recursive_kron(self,list_of_matrices):
        mat = list_of_matrices.pop(0)
        n = len(list_of_matrices)
        for next_item in list_of_matrices:
            mat = np.kron(mat,next_item)
        return mat

    def make_single_operator_list(self,O):
        """Make a list of full-space operators for a given 2x2 operator,
            by taking tensor product with identities on the other 
            excitations
"""
        O_list = []
        for i in range(self.num_sites):
            Oi = self.electronic_identity_kron([(O,i)])
            O_list.append(Oi)
        return O_list

    def make_multi_operator_list(self,o_list):
        """Make a list of full-space operators for a given set of 2x2 
            operators by inserting the necessary identities
"""
        O_list = []
        positions = itertools.combinations(range(self.num_sites),len(o_list))
        for pos_tuple in positions:
            Oi = self.electronic_identity_kron(list(zip(o_list,pos_tuple)))
            O_list.append(Oi)
        return O_list

    def set_occupied_list(self):
        self.occupied_list = self.make_single_operator_list(self.occupied)

    def set_up_list(self):
        self.up_list = self.make_single_operator_list(self.up)

    def set_down_list(self):
        self.down_list = self.make_single_operator_list(self.down)

    def set_empty_list(self):
        self.empty_list = self.make_single_operator_list(self.empty)

    def set_sz_list(self):
        self.sz_list = self.make_single_operator_list(self.sz)
    
    def set_exchange_list(self):
        self.exchange_list = self.make_multi_operator_list([self.up,self.down])

    def get_electronic_hamiltonian(self,*,manifold_num = 'all'):
        if manifold_num == 'all':
            return self.electronic_hamiltonian
        else:
            return self.extract_manifold(self.electronic_hamiltonian,manifold_num)
        
    def optical_dephasing_operator(self):
        total_deph = self.sz_list[0].copy()
        for i in range(1,len(self.sz_list)):
            total_deph += self.sz_list[i]
        return total_deph

    def optical_dephasing_instructions(self):
        O = self.optical_dephasing_operator()
        gamma = self.optical_dephasing_gamma
        return self.make_Lindblad_instructions(gamma,O)

    def optical_dephasing_Liouvillian(self):
        instructions = self.optical_dephasing_instructions()
        return self.make_Liouvillian(instructions)

    def boltzmann_factors(self,E1,E2):
        if E1 == E2:
            return 0.5,0.5

        if E1 < E2:
            return self.boltzmann_factors_ordered_inputs(E1,E2)
        else:
            E1_to_E2, E2_to_E1 = self.boltzmann_factors_ordered_inputs(E2,E1)
            return E2_to_E1, E1_to_E2

    def boltzmann_factors_ordered_inputs(self,E1,E2):
        """E1 must be less than E2"""
        if self.kT == 0:
            return 1, 0
        Z = np.exp(-E1/self.kT) + np.exp(-E2/self.kT)
        if np.isclose(Z,0):
            E2_to_E1 = 1
            E1_to_E2 = 0
        else:
            E2_to_E1 = np.exp(-E1/self.kT)/Z
            E1_to_E2 = np.exp(-E2/self.kT)/Z
        return E2_to_E1, E1_to_E2

    def optical_decoherence_instructions(self):
        eg = 0
        ins_list = []
        gamma = self.optical_decoherence_gamma
        for n in range(len(self.energies)):
            en = self.energies[n]
            bg, bn = self.boltzmann_factors(eg,en)
            O = self.up_list[n]
            instructions2 = self.make_Lindblad_instructions(gamma * bg,O.T)
            ins_list += instructions2
            if np.isclose(bn,0):
                pass
            else:
                instructions1 = self.make_Lindblad_instructions(gamma * bn,O)
                ins_list += instructions1

        return ins_list

    def optical_decoherence_Liouvillian(self):
        inst_list = self.optical_decoherence_instructions()
        L = self.make_Liouvillian(inst_list)
        return L

    def site_to_site_decoherence_instructions(self):
        nm = itertools.combinations(range(len(self.energies)),2)
        i = 0
        ins_list = []
        gamma = self.site_to_site_decoherence_gamma
        for n,m in nm:
            en = self.energies[n]
            em = self.energies[m]
            bn,bm = self.boltzmann_factors(en,em)
            O = self.exchange_list[i]
            instructions1 = self.make_Lindblad_instructions(gamma * bn,O)
            instructions2 = self.make_Lindblad_instructions(gamma * bm,O.T)
            ins_list += instructions1
            ins_list += instructions2
            i+=1

        return ins_list

    def site_to_site_decoherence_Liouvillian(self):
        inst_list = self.site_to_site_decoherence_instructions()
        L = self.make_Liouvillian(inst_list)
        # for i in range(1,len(inst_list)):
        #     L += self.make_Liouvillian(inst_list[i])
        return L

    def site_to_site_dephasing_operator_list(self):
        s_deph_list = []
        for (i,j) in itertools.combinations(range(self.num_sites),2):
            s_deph_list.append(self.sz_list[i] - self.sz_list[j])
        return s_deph_list

    def all_site_dephasing_instructions(self):
        s_deph_list = self.site_to_site_dephasing_operator_list()
        Lindblad_instruction_list = []
        gamma = self.site_to_site_dephasing_gamma
        for O in s_deph_list:
            Lindblad_instruction_list += self.make_Lindblad_instructions(gamma,O)
        return Lindblad_instruction_list

    def all_site_dephasing_Liouvillian(self):
        inst_list = self.all_site_dephasing_instructions()
        L = self.make_Liouvillian(inst_list)
        # for i in range(1,len(inst_list)):
        #     L += self.make_Liouvillian(inst_list[i])
        return L/(2*self.num_sites)

    def exciton_exciton_dephasing_operator_list(self):
        s_deph_list = []
        v = self.electronic_eigenvectors
        for (i,j) in itertools.combinations(range(self.num_sites),2):
            Oi = v.dot(self.sz_list[i].dot(v.T))
            Oj = v.dot(self.sz_list[j].dot(v.T))
            s_deph_list.append(Oi - Oj)
        return s_deph_list

    def all_exciton_dephasing_instructions(self):
        s_deph_list = self.exciton_exciton_dephasing_operator_list()
        Lindblad_instruction_list = []
        gamma = self.exciton_exciton_dephasing_gamma/(2*self.num_sites)
        for O in s_deph_list:
            Lindblad_instruction_list += self.make_Lindblad_instructions(gamma,O)
        return Lindblad_instruction_list

    def all_exciton_dephasing_Liouvillian(self):
        inst_list = self.all_exciton_dephasing_instructions()
        L = self.make_Liouvillian(inst_list)
        # for i in range(1,len(inst_list)):
        #     L += self.make_Liouvillian(inst_list[i])
        return L
    
    @staticmethod
    def make_Lindblad_instructions(gamma,O):
        """O must be square
"""
        II = np.eye(O.shape[0])
        Od = np.conjugate(O.T)
        leftright = gamma * (-np.dot(Od,O)/2)
        return [(gamma*O,Od),(leftright,II),(II,leftright)]

    def make_Lindblad_instructions2(self,gamma,Oket,Obra):
        IIket = np.eye(Oket.shape[0])
        IIbra = np.eye(Obra.shape[0])
        Oketd = np.conjugate(Oket.T)
        Obrad = np.conjugate(Obra.T)
        left = gamma/2 * (-np.dot(Oketd,Oket)/2)
        right = gamma/2 * (-np.dot(Obrad,Obra)/2)
        return [(gamma*Oket/2,Obrad),(left,IIbra),(IIket,right)]

    def make_Lindblad_instructions2_Obra0(self,gamma,Oket,Obra):
        IIbra = np.eye(Obra.shape[0])
        Oketd = np.conjugate(Oket.T)
        left = gamma/2 * (-np.dot(Oketd,Oket)/2)
        return [(left,IIbra)]

    def make_Lindblad_instructions2_Oket0(self,gamma,Oket,Obra):
        IIket = np.eye(Oket.shape[0])
        Obrad = np.conjugate(Obra.T)
        right = gamma/2 * (-np.dot(Obrad,Obra)/2)
        return [(IIket,right)]

    def make_commutator_instructions(self,O):
        """O must be square
"""
        II = np.eye(O.shape[0])
        return [(O,II),(II,-O)]

    def make_commutator_instructions2(self,Oket,Obra):
        """
"""
        IIket = np.eye(Oket.shape[0])
        IIbra = np.eye(Obra.shape[0])
        return [(Oket,IIbra),(IIket,-Obra)]

    def make_manifold_hamiltonian_instructions(self,ket_manifold,bra_manifold):
        Hket = self.get_electronic_hamiltonian(manifold_num = ket_manifold)
        Hbra = self.get_electronic_hamiltonian(manifold_num = bra_manifold)
        return self.make_commutator_instructions2(-1j*Hket,-1j*Hbra)

    @staticmethod
    def make_Liouvillian(instruction_list):
        left, right = instruction_list[0]
        L = np.kron(left,right.T)
        for left,right in instruction_list[1:]:
            L = L + np.kron(left,right.T)
        return L

    def make_total_Liouvillian(self):
        drho = self.make_Liouvillian(self.make_manifold_hamiltonian_instructions('all','all'))
        if self.num_sites > 1:
            drho += self.all_exciton_dephasing_Liouvillian()
            drho += self.exciton_decoherence_Liouvillian()
        # drho += self.optical_decoherence_Liouvillian()
        drho += self.optical_dephasing_Liouvillian()
        
        self.L = drho

    def eigfun(self,L,*,check_eigenvectors = True,invert = True,populations_only = False):
        eigvals, eigvecs = np.linalg.eig(L)

        eigvals = np.round(eigvals,12)
        sort_indices = eigvals.argsort()
        eigvals.sort()
        eigvecs = eigvecs[:,sort_indices]
        for i in range(eigvals.size):
            max_index = np.argmax(np.abs(eigvecs[:,i]))
            if np.real(eigvecs[max_index,i]) < 0:
                eigvecs[:,i] *= -1
            if eigvals[i] == 0:
                # eigenvalues of 0 correspond to thermal distributions,
                # which should have unit trace in the Hamiltonian space
                if populations_only:
                    trace_norm = eigvecs[:,i].sum()
                    eigvecs[:,i] = eigvecs[:,i] / trace_norm
                else:
                    shape = int(np.sqrt(eigvals.size))
                    trace_norm = eigvecs[:,i].reshape(shape,shape).trace()
                    eigvecs[:,i] = eigvecs[:,i] / trace_norm

        if invert:
            eigvecs_left = np.linalg.pinv(eigvecs)
        else:
            eigvals_left, eigvecs_left = np.linalg.eig(L.T)

            eigvals_left = np.round(eigvals_left,12)
            sort_indices_left = eigvals_left.argsort()
            eigvals_left.sort()
            eigvecs_left = eigvecs_left[:,sort_indices_left]
            eigvecs_left = eigvecs_left.T
            for i in range(eigvals_left.size):
                    norm = np.dot(eigvecs_left[i,:],eigvecs[:,i])
                    eigvecs_left[i,:] *= 1/norm

        if check_eigenvectors:
            LV = L.dot(eigvecs)
            D = eigvecs_left.dot(LV)
            if np.allclose(D,np.diag(eigvals),rtol=1E-10,atol=1E-10):
                pass
            else:
                warnings.warn('Using eigenvectors to diagonalize Liouvillian does not result in the expected diagonal matrix to tolerance, largest deviation is {}'.format(np.max(np.abs(D - np.diag(eigvals)))))

        self.eigenvalues = eigvals
        self.eigenvectors = {'left':eigvecs_left,'right':eigvecs}

        return eigvals, eigvecs, eigvecs_left

    def save_L(self,dirname):
        save_npz(os.path.join(dirname,'L.npz'),csr_matrix(self.L))

    def save_L_by_manifold(self):
        np.savez(os.path.join(self.base_path,'L.npz'),**self.L_by_manifold)

    def save_eigsystem(self,dirname):
        np.savez(os.path.join(dirname,'right_eigenvectors.npz'),all_manifolds = self.eigenvectors['right'])
        np.savez(os.path.join(dirname,'left_eigenvectors.npz'),all_manifolds = self.eigenvectors['left'])
        np.savez(os.path.join(dirname,'eigenvalues.npz'),all_manifolds = self.eigenvalues)

    def make_mu_site_basis(self,pol):
        pol_dict = {'x':0,'y':1,'z':2}
        d = self.dipoles[:,pol_dict[pol]]
        self.mu = d[0]*(self.up_list[0] + self.down_list[0])
        for i in range(1,len(self.up_list)):
            self.mu += d[i]*(self.up_list[i] + self.down_list[i])

    def make_mu_dict_site_basis(self):
        self.mu_dict = dict()
        for pol in self.pols:
            self.make_mu_site_basis(pol)
            self.mu_dict[pol] = self.mu.copy()

    def make_mu_up_site_basis(self,pol):
        pol_dict = {'x':0,'y':1,'z':2}
        d = self.dipoles[:,pol_dict[pol]]
        self.mu_ket_up = self.up_list[0].copy()
        for i in range(1,len(self.up_list)):
            self.mu_ket_up += self.up_list[i]

    def save_mu(self,dirname,*,mask=True):
        evl = self.eigenvectors['left']
        ev = self.eigenvectors['right']
        
        II = np.eye(self.mu.shape[0])
        mu_ket = np.kron(self.mu,II.T)
        mu_bra = np.kron(II,self.mu.T)

        mu_mask_tol = 10

        mu_ket_t = np.dot(np.dot(evl,mu_ket),ev)
        mu_ket_3d = np.zeros((mu_ket_t.shape[0],mu_ket_t.shape[0],3),dtype='complex')
        mu_ket_3d[:,:,0] = mu_ket_t

        mu_bra_t = np.dot(np.dot(evl,mu_bra),ev)
        mu_bra_3d = np.zeros((mu_bra_t.shape[0],mu_bra_t.shape[0],3),dtype='complex')
        mu_bra_3d[:,:,0] = mu_bra_t

        if mask:
            ket_mask = np.zeros(mu_ket_t.shape,dtype='bool')
            ket_mask[:,:] = np.round(mu_ket_t,mu_mask_tol)[:,:]
            mu_ket_t_masked = mu_ket_t * ket_mask
            mu_ket_3d_masked = np.zeros((mu_ket_t.shape[0],mu_ket_t.shape[0],3),dtype='complex')
            mu_ket_3d_masked[:,:,0] = mu_ket_t_masked

            bra_mask = np.zeros(mu_bra_t.shape,dtype='bool')
            bra_mask[:,:] = np.round(mu_bra_t,mu_mask_tol)[:,:]
            mu_bra_t_masked = mu_bra_t * bra_mask
            mu_bra_3d_masked = np.zeros((mu_ket_t.shape[0],mu_ket_t.shape[0],3),dtype='complex')
            mu_bra_3d_masked[:,:,0] = mu_bra_t_masked

            np.savez(os.path.join(dirname,'mu.npz'),ket=mu_ket_3d,bra=mu_bra_3d)
            np.savez(os.path.join(dirname,'eigenvalues.npz'),all_manifolds=self.eigenvalues)
            np.savez(os.path.join(dirname,'right_eigenvectors.npz'),all_manifolds=ev)
            np.savez(os.path.join(dirname,'left_eigenvectors.npz'),all_manifolds=evl)
            np.savez(os.path.join(dirname,'mu_boolean.npz'),ket=ket_mask,bra=bra_mask)
            np.savez(os.path.join(dirname,'mu_pruned.npz'),ket=mu_ket_3d_masked,bra=mu_bra_3d_masked)

        else:
            np.savez(os.path.join(dirname,'mu.npz'),ket=mu_ket_3d,bra=mu_bra_3d)
            np.savez(os.path.join(dirname,'eigenvalues.npz'),all_manifolds=self.eigenvalues)
            np.savez(os.path.join(dirname,'right_eigenvectors.npz'),all_manifolds=ev)
            np.savez(os.path.join(dirname,'left_eigenvectors.npz'),all_manifolds=evl)

    def save_RWA_mu(self,dirname,*,mask=True):
        evl = self.eigenvectors['left']
        ev = self.eigenvectors['right']
        
        II = np.eye(self.mu_ket_up.shape[0])

        mu_ket_up = np.kron(self.mu_ket_up,II.T)
        mu_ket_down = np.kron(self.mu_ket_up.T,II.T)
        mu_bra_up = np.kron(II,self.mu_ket_up)
        mu_bra_down = np.kron(II,self.mu_ket_up.T)

        mu_mask_tol = 10
        
        mu_ket_up_t = np.dot(np.dot(evl,mu_ket_up),ev)
        mu_ket_up_3d = np.zeros((mu_ket_up_t.shape[0],mu_ket_up_t.shape[0],3),dtype='complex')
        mu_ket_up_3d[:,:,0] = mu_ket_up_t

        mu_bra_up_t = np.dot(np.dot(evl,mu_bra_up),ev)
        mu_bra_up_3d = np.zeros((mu_bra_up_t.shape[0],mu_bra_up_t.shape[0],3),dtype='complex')
        mu_bra_up_3d[:,:,0] = mu_bra_up_t

        mu_ket_down_t = np.dot(np.dot(evl,mu_ket_down),ev)
        mu_ket_down_3d = np.zeros((mu_ket_down_t.shape[0],mu_ket_down_t.shape[0],3),dtype='complex')
        mu_ket_down_3d[:,:,0] = mu_ket_down_t

        mu_bra_down_t = np.dot(np.dot(evl,mu_bra_down),ev)
        mu_bra_down_3d = np.zeros((mu_bra_down_t.shape[0],mu_bra_down_t.shape[0],3),dtype='complex')
        mu_bra_down_3d[:,:,0] = mu_bra_down_t

        if mask:
            ket_up_mask = np.zeros(mu_ket_up_t.shape,dtype='bool')
            ket_up_mask[:,:] = np.round(mu_ket_up_t,mu_mask_tol)[:,:]
            mu_ket_up_t_masked = mu_ket_up_t * ket_up_mask
            mu_ket_up_3d_masked = np.zeros((mu_ket_up_t.shape[0],mu_ket_up_t.shape[0],3),dtype='complex')
            mu_ket_up_3d_masked[:,:,0] = mu_ket_up_t_masked

            bra_up_mask = np.zeros(mu_bra_up_t.shape,dtype='bool')
            bra_up_mask[:,:] = np.round(mu_bra_up_t,mu_mask_tol)[:,:]
            mu_bra_up_t_masked = mu_bra_up_t * bra_up_mask
            mu_bra_up_3d_masked = np.zeros((mu_ket_up_t.shape[0],mu_ket_up_t.shape[0],3),dtype='complex')
            mu_bra_up_3d_masked[:,:,0] = mu_bra_up_t_masked

            ket_down_mask = np.zeros(mu_ket_down_t.shape,dtype='bool')
            ket_down_mask[:,:] = np.round(mu_ket_down_t,mu_mask_tol)[:,:]
            mu_ket_down_t_masked = mu_ket_down_t * ket_down_mask
            mu_ket_down_3d_masked = np.zeros((mu_ket_down_t.shape[0],mu_ket_down_t.shape[0],3),dtype='complex')
            mu_ket_down_3d_masked[:,:,0] = mu_ket_down_t_masked

            bra_down_mask = np.zeros(mu_bra_down_t.shape,dtype='bool')
            bra_down_mask[:,:] = np.round(mu_bra_down_t,mu_mask_tol)[:,:]
            mu_bra_down_t_masked = mu_bra_down_t * bra_down_mask
            mu_bra_down_3d_masked = np.zeros((mu_ket_down_t.shape[0],mu_ket_down_t.shape[0],3),dtype='complex')
            mu_bra_down_3d_masked[:,:,0] = mu_bra_down_t_masked

            np.savez(os.path.join(dirname,'mu.npz'),ket_up=mu_ket_up_3d,bra_up=mu_bra_up_3d,
                     ket_down=mu_ket_down_3d,bra_down=mu_bra_down_3d)
            np.savez(os.path.join(dirname,'eigenvalues.npz'),all_manifolds=self.eigenvalues)
            np.savez(os.path.join(dirname,'right_eigenvectors.npz'),all_manifolds=ev)
            np.savez(os.path.join(dirname,'left_eigenvectors.npz'),all_manifolds=evl)
            np.savez(os.path.join(dirname,'mu_boolean.npz'),ket_up=ket_up_mask,bra_up=bra_up_mask,
                     ket_down=ket_down_mask,bra_down=bra_down_mask)
            np.savez(os.path.join(dirname,'mu_pruned.npz'),ket_up=mu_ket_up_3d_masked,
                     bra_up=mu_bra_up_3d_masked,ket_down=mu_ket_down_3d_masked,
                     bra_down=mu_bra_down_3d_masked)

        else:
            np.savez(os.path.join(dirname,'mu.npz'),ket_up=mu_ket_up_3d,bra_up=mu_bra_up_3d,
                     ket_down=mu_ket_down_3d,bra_down=mu_bra_down_3d)
            np.savez(os.path.join(dirname,'eigenvalues.npz'),all_manifolds=self.eigenvalues)
            np.savez(os.path.join(dirname,'right_eigenvectors.npz'),all_manifolds=ev)
            np.savez(os.path.join(dirname,'left_eigenvectors.npz'),all_manifolds=evl)

    def save_RWA_mu_site_basis(self,dirname):
        
        II = np.eye(self.mu_ket_up.shape[0])
        mu_ket_up = np.kron(self.mu_ket_up,II.T)
        mu_ket_down = np.kron(self.mu_ket_up.T,II.T)
        mu_bra_up = np.kron(II,self.mu_ket_up)
        mu_bra_down = np.kron(II,self.mu_ket_up.T)

        mu_mask_tol = 10
        
        mu_ket_up_3d = np.zeros((mu_ket_up.shape[0],mu_ket_up.shape[0],3),dtype='complex')
        mu_ket_up_3d[:,:,0] = mu_ket_up

        mu_bra_up_3d = np.zeros((mu_bra_up.shape[0],mu_bra_up.shape[0],3),dtype='complex')
        mu_bra_up_3d[:,:,0] = mu_bra_up

        mu_ket_down_3d = np.zeros((mu_ket_down.shape[0],mu_ket_down.shape[0],3),dtype='complex')
        mu_ket_down_3d[:,:,0] = mu_ket_down

        mu_bra_down_3d = np.zeros((mu_bra_down.shape[0],mu_bra_down.shape[0],3),dtype='complex')
        mu_bra_down_3d[:,:,0] = mu_bra_down

        np.savez(os.path.join(dirname,'mu_site_basis.npz'),ket_up=mu_ket_up_3d,bra_up=mu_bra_up_3d,
                     ket_down=mu_ket_down_3d,bra_down=mu_bra_down_3d)

    def make_manifold_eigensystem(self,manifold_num):
        h = self.get_electronic_hamiltonian(manifold_num = manifold_num)
        e, v = np.linalg.eigh(h)
        sort_inds = e.argsort()
        e = e[sort_inds]
        v = v[:,sort_inds]
        return e,v

    def set_manifold_eigensystems(self):
        self.electronic_eigenvalues_by_manifold = []
        self.electronic_eigenvectors_by_manifold = []
        for i in range(self.num_sites+1):
            e,v = self.make_manifold_eigensystem(i)
            self.electronic_eigenvalues_by_manifold.append(e)
            self.electronic_eigenvectors_by_manifold.append(v)

    def get_eigensystem_by_manifold(self,manifold_num):
        e = self.electronic_eigenvalues_by_manifold[manifold_num]
        v = self.electronic_eigenvectors_by_manifold[manifold_num]
        return e,v

    def set_electronic_eigensystem(self):
        H = self.electronic_hamiltonian
        eigvecs = np.zeros(H.shape)
        d = np.zeros(H.shape)
        for i in range(self.num_sites+1):
            e,v = self.get_eigensystem_by_manifold(i)
            if i ==1:
                self.exciton_energies = e
            eigvecs += self.manifold_to_full(v,i)
            d += self.manifold_to_full(np.diag(e),i)
        Hd = eigvecs.T.dot(H.dot(eigvecs))
        if np.allclose(Hd,d):
            pass
        else:
            raise Exception('Diagonalization by manifold failed')

        self.electronic_eigenvectors = eigvecs
        self.electronic_eigenvalues = d.diagonal()

    def exciton_decoherence_instructions_by_manifold(self,manifold_num,*,full_space = True):
        e,v = self.get_eigensystem_by_manifold(manifold_num)
        ins_list = []
        gamma = self.exciton_decoherence_gamma

        for m,n in itertools.combinations(range(e.size),2):
            exchange = np.zeros((e.size,e.size))
            em = e[m]
            en = e[n]
            bm,bn = self.boltzmann_factors(em,en)
            
            exchange[m,n] = 1

            O = v.dot(exchange.dot(v.T))
            if full_space:
                O = self.manifold_to_full(O,manifold_num)
            instructions1 = self.make_Lindblad_instructions(gamma * bm,O)
            instructions2 = self.make_Lindblad_instructions(gamma * bn,O.T)
            ins_list += instructions1
            ins_list += instructions2

        return ins_list

    def exciton_decoherence_Liouvillian(self):
        inst_list = self.exciton_decoherence_instructions_by_manifold(1)
        for i in range(2,self.num_sites+1):
            inst_list += self.exciton_decoherence_instructions_by_manifold(i)
        L = self.make_Liouvillian(inst_list)
        # for i in range(1,len(inst_list)):
        #     L += self.make_Liouvillian(inst_list[i])
        return L

    def exciton_decoherence_Liouvillian_manifold(self,manifold_num,full_space=False):
        inst_list = self.exciton_decoherence_instructions_by_manifold(manifold_num,full_space=full_space)
        L = self.make_Liouvillian(inst_list)
        # for i in range(1,len(inst_list)):
        #     L += self.make_Liouvillian(inst_list[i])
        return L
    
    def exciton_decoherence_instructions2(self):
        nm = itertools.combinations(range(len(self.exciton_energies)),2)
        i = 0
        ins_list = []
        gamma = self.exciton_decoherence_gamma

        v = self.electronic_eigenvectors
        for n,m in nm:
            en = self.exciton_energies[n]
            em = self.exciton_energies[m]
            bn, bm = self.boltzmann_factors(en,em)
            O = v.dot(self.exchange_list[i].dot(v.T))
            instructions1 = self.make_Lindblad_instructions(gamma * bn,O)
            instructions2 = self.make_Lindblad_instructions(gamma * bm,O.T)
            ins_list += instructions1
            ins_list += instructions2
            i+=1

        return ins_list

    def exciton_decoherence_Liouvillian2(self):
        inst_list = self.exciton_decoherence_instructions2()
        L = self.make_Liouvillian(inst_list)
        # for i in range(1,len(inst_list)):
        #     L += self.make_Liouvillian(inst_list[i])
        return L

    def set_electronic_dissipation_instructions(self):
        inst_list = self.optical_dephasing_instructions()
        if self.exciton_exciton_dephasing_gamma != 0 and self.site_to_site_dephasing_gamma != 0:
            warnings.warn('Both exciton and site dephasing are being included')
        if self.exciton_decoherence_gamma != 0 and self.site_to_site_decoherence_gamma != 0:
            warnings.warn('Both exciton and site relaxation are being included')

        if self.site_to_site_dephasing_gamma != 0:
            inst_list += self.all_site_dephasing_instructions()
        if self.site_to_site_decoherence_gamma != 0:
            inst_list += self.site_to_site_decoherence_instructions()
            
        if self.exciton_exciton_dephasing_gamma != 0:
            inst_list += self.all_exciton_dephasing_instructions()
        if self.exciton_decoherence_gamma != 0:
            inst_list += self.exciton_decoherence_instructions_by_manifold(1)
            for i in range(2,self.num_sites+1):
                inst_list += self.exciton_decoherence_instructions_by_manifold(i)
        if self.optical_decoherence_gamma != 0:
            inst_list += self.optical_decoherence_instructions()
        self.electronic_dissipation_instructions = inst_list

    def set_electronic_dissipation_instructions2(self):
        inst_list = self.optical_dephasing_instructions()
        inst_list += self.all_site_dephasing_instructions()
        inst_list += self.site_to_site_decoherence_instructions()
        if self.optical_decoherence_gamma != 0:
            inst_list += self.optical_decoherence_instructions()
        self.electronic_dissipation_instructions = inst_list
            

class OpenPolymer2(OpenPolymer):
    def __init__(self,site_energies,site_couplings):
        super().__init__(site_energies,site_couplings)
        self.set_diagonal_polymer()
        
    def set_diagonal_polymer(self):
        self.exciton_energies = np.linalg.eigvalsh(self.get_electronic_hamiltonian(manifold_num=1))
        self.exciton_energies.sort()
        self.diagonal_polymer = OpenPolymer(self.exciton_energies,np.zeros(len(self.couplings)))
        diagonal = self.diagonal_polymer.electronic_hamiltonian.diagonal()
        diagonal_sort_inds = diagonal.argsort()
        diagonal_unsort_inds = diagonal_sort_inds.argsort()
        diagonal_sorted = diagonal[diagonal_sort_inds]
        
        e, v = np.linalg.eigh(self.electronic_hamiltonian)
        e_sort_inds = e.argsort()
        e = e[e_sort_inds]
        v = v[:,e_sort_inds]
        e = e[diagonal_unsort_inds]
        v = v[:,diagonal_unsort_inds]

        diagonal2 = (v.T.dot(self.electronic_hamiltonian.dot(v))).diagonal()
        if np.allclose(diagonal,diagonal2,rtol = 1E-10, atol = 1E-10):
            pass
        else:
            print(diagonal2)
            print(diagonal)
            warnings.warn('Failed to find and sort electronic space eigenvectors')

        self.electronic_eigenvalues = e
        self.electronic_eigenvectors = v

    def exciton_decoherence_instructions(self):
        nm = itertools.combinations(range(len(self.exciton_energies)),2)
        i = 0
        ins_list = []
        gamma = self.exciton_decoherence_gamma

        v = self.electronic_eigenvectors
        for n,m in nm:
            en = self.exciton_energies[n]
            em = self.exciton_energies[m]
            bn, bm = self.boltzmann_factors(en,em)
            O = v.dot(self.exchange_list[i].dot(v.T))
            instructions1 = self.make_Lindblad_instructions(gamma * bn,O)
            instructions2 = self.make_Lindblad_instructions(gamma * bm,O.T)
            ins_list.append(instructions1)
            ins_list.append(instructions2)
            i+=1
            print(n,m,bn,bm)

        return ins_list

    def exciton_decoherence_Liouvillian(self):
        inst_list = self.exciton_decoherence_instructions()
        L = self.make_Liouvillian(inst_list[0])
        for i in range(1,len(inst_list)):
            L += self.make_Liouvillian(inst_list[i])
        return L

    def make_total_Liouvillian(self):
        drho = self.make_Liouvillian(self.make_manifold_hamiltonian_instructions('all','all'))
        drho += self.all_exciton_dephasing_Liouvillian()
        drho += self.exciton_decoherence_Liouvillian()
        drho += self.optical_decoherence_Liouvillian()
        drho += self.optical_dephasing_Liouvillian()

        self.L = drho


class OpenPolymerVibrations(OpenPolymer):
    def __init__(self,yaml_file,*,mask_by_occupation_num=True,force_detailed_balance=False,for_RK=False):
        """Initial set-up is the same as for the Polymer class, but I also need
to unpack the vibrational_frequencies, which must be passed as a nested list.
Each site may have N vibrational modes, and each has a frequency, a displacement
and a frequency shift for the excited state
for sites a, b, ...
"""
        with open(yaml_file) as yamlstream:
            params = yaml.load(yamlstream,Loader=yaml.SafeLoader)
        self.base_path = os.path.split(yaml_file)[0]
        super().__init__(params['site_energies'],params['site_couplings'],np.array(params['dipoles']))

        self.H_diagonalization_time = 0
        self.L_diagonalization_time = 0
        self.L_construction_time = 0
        
        self.truncation_size = params['initial truncation size']
        try:
            self.maximum_manifold = params['maximum_manifold']
        except:
            self.maximum_manifold = np.inf
        self.maximum_manifold = min(self.maximum_manifold,self.num_sites)
        self.params = params

        self.set_bath_coupling()

        if self.optical_decoherence_gamma != 0:
            self.manifolds_separable = False
        else:
            self.manifolds_separable = True
        
        self.set_electronic_dissipation_instructions()
        
        self.occupation_num_mask = mask_by_occupation_num
        self.set_vibrations()
        self.set_vibrational_ladder_operators()
        
        if self.manifolds_separable == True:
            e_ham = self.electronic_hamiltonian
        else:
            e_ham = self.extract_electronic_subspace(self.electronic_hamiltonian,0,self.maximum_manifold)
            
        self.total_hamiltonian = np.kron(e_ham,self.vibrational_identity)
        self.add_vibrations()

        t0 = time.time()
        self.set_H_eigsystem_by_manifold()
        self.H_diagonalization_time = time.time() - t0
        
        self.make_condon_mu()
        self.make_condon_mu_dict()

        if force_detailed_balance:
            H_eigentransform = True
            t0 = time.time()
            self.all_instructions = self.make_commutator_instructions(-1j*self.total_hamiltonian)
            self.set_L_by_manifold(H_eigentransform=H_eigentransform,add_eigenstate_decoherence_effects = False)
            self.add_eigenstate_decoherence_effects()
            self.add_eigenstate_optical_dephasing_effects()
            self.L_construction_time = time.time() - t0
        else:
            H_eigentransform = False
            t0 = time.time()
            self.all_instructions = self.convert_electronic_instructions_to_full_instructions(self.electronic_dissipation_instructions)
            self.all_instructions += self.make_commutator_instructions(-1j*self.total_hamiltonian)
            self.all_instructions += self.vibrational_dissipation_instructions()
            if self.manifolds_separable:
                self.set_L_by_manifold(H_eigentransform=H_eigentransform)
            else:
                self.set_L()
            self.L_construction_time = time.time() - t0

        if for_RK:
            self.set_mu_by_manifold(H_eigentransform=H_eigentransform,L_eigentransform=False)
            self.save_mu_by_manifold(pruned=False)
            self.save_L_by_manifold()
            self.save_rho0(H_eigentransform=H_eigentransform)
            
        else:
            t0 = time.time()
            if self.manifolds_separable:
                self.set_eigensystem_by_manifold(force_detailed_balance = force_detailed_balance)
                self.set_mu_by_manifold(H_eigentransform=H_eigentransform)
                self.save_mu_by_manifold(pruned=True)
                self.save_eigensystem_by_manifold()
                self.L_diagonalization_time = time.time() - t0
                
            else:
                self.set_eigensystem()
                # self.set_mu()
                # self.save_mu(pruned=True)
                # self.save_eigensystem()
                # self.L_diagonalization_time = time.time() - t0
                

        self.save_timings()

    def save_timings(self):
        save_dict = {'H_diagonalization_time':self.H_diagonalization_time,
                     'L_diagonalization_time':self.L_diagonalization_time,
                     'L_construction_time':self.L_construction_time}
        np.savez(os.path.join(self.base_path,'Liouvillian_timings.npz'),**save_dict)

    def set_H_eigsystem_by_manifold(self):
        self.H_eigenvalues = []
        self.H_eigenvectors = []
        for i in range(self.maximum_manifold+1):
            e,v = np.linalg.eigh(self.extract_vibronic_manifold(self.total_hamiltonian,i))
            for i in range(e.size):
                max_ind = np.argmax(np.abs(v[:,i]))
                if v[max_ind,i] < 0:
                    v[:,i] = v[:,i] * -1
            self.H_eigenvalues.append(e)
            self.H_eigenvectors.append(v)

    def save_rho0(self,*,H_eigentransform=False):
        H_size = self.H_eigenvalues[0].size
        if H_size == 1:
            rho0 = np.array([[1]])
        elif self.kT == 0:
            rho0 = np.zeros((H_size,H_size))
            rho0[0,0] = 1
        else:
            Z = np.sum(np.exp(-self.H_eigenvalues[0]/self.kT))
            rho0_diag = np.exp(-self.H_eigenvalues[0]/self.kT)/Z
            rho0 = np.diag(rho0_diag)

        if H_eigentransform:
            # Already in the eigenbasis
            pass
        else:
            # Go back to original basis
            v = self.H_eigenvectors[0]
            rho0 = v.dot(rho0.dot(v.T))

        rho0 = rho0.flatten()
        np.save(os.path.join(self.base_path,'rho0.npy'),rho0)
            

    def eigfun2(self,ket_manifold_num,bra_manifold_num,*,check_eigenvectors = True):
        key = str(ket_manifold_num) + str(bra_manifold_num)
        L = self.L_by_manifold[key]
        E = L.diagonal().copy()
        V = np.eye(E.size,dtype='complex')
        VL = V.copy()
        
        if ket_manifold_num == bra_manifold_num:
            size = self.H_eigenvalues[ket_manifold_num].size
            pop_inds = np.arange(size)*(size+1)
            L_pop = L[pop_inds,:]
            L_pop = L_pop[:,pop_inds]
            e, v, vl = self.eigfun(L_pop,populations_only=True)
            E[pop_inds] = e[:]
            for i,j in zip(pop_inds,range(len(pop_inds))):
                
                V[pop_inds,i] = v[:,j]
                
                VL[pop_inds,i] = vl[:,j]

        if check_eigenvectors:
            # L = csr_matrix(L)
            # V = csc_matrix(V)
            # VL = csr_matrix(VL)
            LV = L.dot(V)
            D = VL.dot(LV)
            if np.allclose(D,np.diag(E),rtol=1E-10,atol=1E-10):
                pass
            else:
                warnings.warn('Using eigenvectors to diagonalize Liouvillian does not result in the expected diagonal matrix to tolerance, largest deviation is {}'.format(np.max(np.abs(D - np.diag(E)))))

        self.eigenvalues = E
        self.eigenvectors = {'left':VL,'right':V}

        return E,V,VL

    def vibrational_occupation_to_indices(self,vibration,occ_num,manifold_num):
        single_mode_occ = np.arange(self.truncation_size)
        vib_occ = self.vibrational_vector_of_ones_kron(vibration,single_mode_occ)
        masked_single_mode_occ = vib_occ[self.vibrational_mask]

        electronic_manifold_hamiltonian = self.get_electronic_hamiltonian(manifold_num = manifold_num)
        elec_size = electronic_manifold_hamiltonian.shape[0]
        
        masked_single_mode_occ = np.kron(np.ones(elec_size),masked_single_mode_occ)
        return np.where(masked_single_mode_occ == occ_num)[0]

    def electronic_occupation_to_indices(self,site_num,manifold_num):
        single_mode_occ = np.arange(2)
        elec_occ = self.electronic_vector_of_ones_kron(site_num,single_mode_occ)
        mask = self.electronic_manifold_mask(manifold_num)
        masked_elec_occ = elec_occ[mask]
        masked_elec_occ = np.kron(masked_elec_occ,np.ones(self.vibrational_mask[0].size))

        return np.where(masked_elec_occ == 1)[0]

    def get_vibrational_decoherence_rates(self,manifold_num):
        e = self.H_eigenvalues[manifold_num]
        rates = np.zeros((e.size,e.size))
        for i in range(e.size):
            for j in range(e.size):
                for n in range(self.num_vibrations):
                # for n in range(1):
                    if j > i:
                        rates[i,j] += self.single_vibrational_decoherence_rate(i,j,n,manifold_num)
        return rates
    
    def single_vibrational_decoherence_rate(self,i,j,vibration,manifold_num):
        vi = self.H_eigenvectors[manifold_num][:,i]
        vj = self.H_eigenvectors[manifold_num][:,j]
        rate = 0
        for k in range(self.truncation_size):
            k_inds = self.vibrational_occupation_to_indices(vibration,k,manifold_num)
            kp1_inds = self.vibrational_occupation_to_indices(vibration,k+1,manifold_num)
            for k_ind,kp1_ind in zip(k_inds,kp1_inds):
                rate = rate + np.abs(vi[k_ind])**2 * np.abs(vj[kp1_ind])**2*np.sqrt(k+1)
        return rate

    def get_electronic_decoherence_rates(self,a,b,manifold_num):
        e = self.H_eigenvalues[manifold_num]
        rates = np.zeros((e.size,e.size))
        for i in range(e.size):
            for j in range(e.size):
                if j > i:
                    rates[i,j] += self.single_electronic_decoherence_rate(i,j,a,b,manifold_num)
        return rates

    def get_all_electronic_decoherence_rates(self,manifold_num):
        """Treats all sites as having the same decoherence rates
"""
        e = self.H_eigenvalues[manifold_num]
        rates = np.zeros((e.size,e.size))
        for i in range(e.size):
            for j in range(e.size):
                if j > i:
                    for a in range(len(self.energies)):
                        Ea = self.energies[a]
                        for b in range(len(self.energies)):
                            Eb = self.energies[b]
                            if Eb > Ea:
                                rates[i,j] += self.single_electronic_decoherence_rate(i,j,a,b,manifold_num)
        return rates

    def get_all_decoherence_rates(self,manifold_num):
        rates = self.vibrational_gamma * self.get_vibrational_decoherence_rates(manifold_num)
        rates = rates + self.site_to_site_decoherence_gamma * self.get_all_electronic_decoherence_rates(manifold_num)
        return rates

    def all_eigenstate_decoherence_instructions_by_manifold(self,manifold_num):
        rates = self.get_all_decoherence_rates(manifold_num)
        E = self.H_eigenvalues[manifold_num]
        ins = []
        for i in range(rates.shape[0]):
            for j in range(rates.shape[1]):
                if j > i:
                    O = np.zeros(rates.shape)
                    O[i,j] = 1
                    down, up = self.boltzmann_factors(E[i],E[j])
                    down = down * rates[i,j]
                    up = up * rates[i,j]
                    ins += self.make_Lindblad_instructions(down,O)
                    if np.isclose(up,0):
                        pass
                    else:
                        ins += self.make_Lindblad_instructions(up,O.T)
        return ins

    def all_eigenstate_decoherence_instructions_by_coherence(self,ket_manifold_num,bra_manifold_num):
        if ket_manifold_num == bra_manifold_num:
            return self.all_eigenstate_decoherence_instructions_by_manifold(ket_manifold_num)
        ket_rates = self.get_all_decoherence_rates(ket_manifold_num)
        E_ket = self.H_eigenvalues[ket_manifold_num]
        bra_rates = self.get_all_decoherence_rates(bra_manifold_num)
        E_bra = self.H_eigenvalues[bra_manifold_num]
        ins = []
        Obra = np.zeros(bra_rates.shape)
        for i in range(ket_rates.shape[0]):
            for j in range(ket_rates.shape[1]):
                if j > i:
                    Oket = np.zeros(ket_rates.shape)
                    Oket[i,j] = 1
                    down,up = self.boltzmann_factors(E_ket[i],E_ket[j])
                    down = down * ket_rates[i,j]
                    up = up * ket_rates[i,j]
                        
                    # IIbra = np.eye(Obra.shape[0])
                    # Oketd = Oket.T #conjugate unnecessary for this
                    # left = down/2 * (-np.dot(Oketd,Oket)/2)
                    # ins += [(left,IIbra)]
                    ins += self.make_Lindblad_instructions2_Obra0(down,Oket,Obra)

                    if np.isclose(up,0):
                        pass
                    else:
                        ins += self.make_Lindblad_instructions2_Obra0(up,Oket.T,Obra)

        Oket = np.zeros(ket_rates.shape)
        for i in range(bra_rates.shape[0]):
            for j in range(bra_rates.shape[1]):
                if j > i:
                    Obra = np.zeros(bra_rates.shape)
                    Obra[i,j] = 1
                    down,up = self.boltzmann_factors(E_bra[i],E_bra[j])
                    down = down * bra_rates[i,j]
                    up = up * bra_rates[i,j]
                        
                    # IIket = np.eye(Oket.shape[0])
                    # Obrad = Obra.T #conjugate unnecessary for this
                    # right = 
                    # ins += [(IIket,right)]
                    ins += self.make_Lindblad_instructions2_Oket0(down,Oket,Obra)

                    if np.isclose(up,0):
                        pass
                    else:
                        ins += self.make_Lindblad_instructions2_Oket0(up,Oket,Obra.T)
        return ins

    # def add_eigenstate_decoherence_effects(self):
    #     for k in range(self.maximum_manifold+1):
    #         rates_k = self.get_all_decoherence_rates(k)
    #         for l in range(self.maximum_manifold+1):
    #             rates_l = self.get_all_decoherence_rates(l)
    #             key = str(k) + str(l)
    #             L = self.L_by_manifold[key]
    #             if k == l:
    #                 for i in range(rates_k.shape[0]):
    #                     for j in range(rates_k.shape[1]):
    #                         if j > i:
    #                             L += self.make_eigenstate_decoherence_Lindblad(rates_k[i,j],i,j,k)
    #                         else:
    #                             pass
    #             else:
    #                 for i in range(rates_k.shape[0]):
    #                     for j in range(rates_k.shape[1]):
    #                         if j > i:
    #                             L += self.make_eigenstate_decoherence_Lindblad_optical_coherence(rates_k[i,j],i,j,k,l,
    #                                                                                                decoherence_in_ket = True)
    #                         else:
    #                             pass

    #                 for i in range(rates_l.shape[0]):
    #                     for j in range(rates_l.shape[1]):
    #                         if j > i:
    #                             L += self.make_eigenstate_decoherence_Lindblad_optical_coherence(rates_l[i,j],i,j,k,l,
    #                                                                                                decoherence_in_ket = False)
    #                         else:
    #                             pass
                            
    def single_electronic_decoherence_rate(self,i,j,a,b,manifold_num):
        vi = self.H_eigenvectors[manifold_num][:,i]
        vj = self.H_eigenvectors[manifold_num][:,j]
        a_inds = self.electronic_occupation_to_indices(a,manifold_num)
        b_inds = self.electronic_occupation_to_indices(b,manifold_num)
        rate = np.sum(np.abs(vi[a_inds])**2) * np.sum(np.abs(vj[b_inds])**2)

        return rate

    def make_eigenstate_decoherence_Lindblad_all_rates(self,rates,manifold_num):
        """From j to i. Factor of 0.5 matches my previous definition of Lindblad formalism"""
        E = self.H_eigenvalues[manifold_num]
        size = E.size
        pop_inds = np.arange(size)*(size+1)
        pop_subspace = np.zeros((pop_inds.size,pop_inds.size))
        L_diagonal = np.zeros((size,size))
        
        for i in range(size):
            for j in range(size):
                if j > i:
                    down,up = self.boltzmann_factors(E[i],E[j])
                    down = down * rates[i,j]
                    up = up * rates[i,j]

                    pop_subspace[j,j] += -0.5*down
                    pop_subspace[i,j] += 0.5*down
                    pop_subspace[i,i] += -0.5*up
                    pop_subspace[j,i] += 0.5*up

                    L_diagonal[j,:] += -0.25*down
                    L_diagonal[:,j] += -0.25*down
                    L_diagonal[j,j] += -0.5*down

                    L_diagonal[i,:] += -0.25*up
                    L_diagonal[:,i] += -0.25*up
                    L_diagonal[i,i] += -0.5*up
                    
        L_total = np.diag(L_diagonal.ravel())
        for i,j in zip(pop_inds,np.arange(pop_inds.size)):
            L_total[i,pop_inds] = pop_subspace[j,:]

        return L_total

    def make_eigenstate_decoherence_Lindblad_all_rates_by_coherence(self,ket_rates,bra_rates,ket_manifold_num,bra_manifold_num):
        """From j to i. Factor of 0.5 matches my previous definition of Lindblad formalism"""
        if ket_manifold_num == bra_manifold_num:
            return self.make_eigenstate_decoherence_Lindblad_all_rates(ket_rates,ket_manifold_num)
        E_ket = self.H_eigenvalues[ket_manifold_num]
        E_bra = self.H_eigenvalues[bra_manifold_num]
        ket_size = E_ket.size
        bra_size = E_bra.size
        L_diagonal = np.zeros((ket_size,bra_size))
        
        for i in range(ket_size):
            for j in range(ket_size):
                if j > i:
                    down,up = self.boltzmann_factors(E_ket[i],E_ket[j])
                    down = down * ket_rates[i,j]
                    up = up * ket_rates[i,j]

                    L_diagonal[j,:] += -0.25*down
                    L_diagonal[i,:] += -0.25*up

        for i in range(bra_size):
            for j in range(bra_size):
                if j > i:
                    down,up = self.boltzmann_factors(E_bra[i],E_bra[j])
                    down = down * bra_rates[i,j]
                    down = down * bra_rates[i,j]

                    L_diagonal[:,j] += -0.25*down
                    L_diagonal[:,i] += -0.25*up
                    
        L_total = np.diag(L_diagonal.ravel())

        return L_total

    def add_eigenstate_decoherence_effects(self):
        for k in range(self.maximum_manifold+1):
            rates_k = self.get_all_decoherence_rates(k)
            for l in range(self.maximum_manifold+1):
                rates_l = self.get_all_decoherence_rates(l)
                key = str(k) + str(l)
                L = self.L_by_manifold[key]
                L += self.make_eigenstate_decoherence_Lindblad_all_rates_by_coherence(rates_k,rates_l,k,l)

    def add_eigenstate_optical_dephasing_effects(self):
        for k in range(self.maximum_manifold+1):
            for l in range(self.maximum_manifold+1):
                if k == l:
                    pass
                else:
                    key = str(k) + str(l)
                    L = self.L_by_manifold[key]
                    L += self.make_eigenstate_optical_dephasing_Lindblad(k,l)

    def make_eigenstate_decoherence_Lindblad(self,gamma,i,j,manifold_num):
        """From j to i. Factor of 0.5 matches my previous definition of Lindblad formalism"""
        size = self.H_eigenvalues[manifold_num].size
        pop_inds = np.arange(size)*(size+1)
        pop_subspace = np.zeros((pop_inds.size,pop_inds.size))
        pop_subspace[j,j] = -0.5
        pop_subspace[i,j] = 0.5

        L_diagonal = np.zeros((size,size))
        L_diagonal[j,:] = -0.25
        L_diagonal[:,j] = -0.25
        L_diagonal[j,j] = -0.5
        L_total = np.diag(L_diagonal.ravel())
        for i,j in zip(pop_inds,np.arange(pop_inds.size)):
            L_total[i,pop_inds] = pop_subspace[j,:]

        return gamma*L_total

    def make_eigenstate_decoherence_Lindblad_optical_coherence(self,gamma,i,j,ket_manifold_num,bra_manifold_num,*,
                                                              decoherence_in_ket = True):
        """From j to i. Factor of 0.25 matches my previous definition of Lindblad formalism"""
        ket_size = self.H_eigenvalues[ket_manifold_num].size
        bra_size = self.H_eigenvalues[bra_manifold_num].size

        L_diagonal = np.zeros((ket_size,bra_size))
        if decoherence_in_ket:
            L_diagonal[j,:] = -0.25
        else:
            L_diagonal[:,j] = -0.25
        L_total = np.diag(L_diagonal.ravel())

        return gamma*L_total

    def make_eigenstate_optical_dephasing_Lindblad(self,ket_manifold_num,bra_manifold_num):
        """Use a constant dephasing rate for all states: my best idea is to
createe the dephasing Lindblad for the electronic space only, and use it to 
fill in a single rate on the diagonal of the Liouvillian.  The trick is to get
dephasing between the nth and n+kth manifold right, when k > 1 (k = 1 is simply 
gamma)"""
        opt_deph = self.optical_dephasing_Liouvillian().diagonal().reshape(self.electronic_hamiltonian.shape)
        
        opt_deph = self.extract_coherence(opt_deph,ket_manifold_num,bra_manifold_num).ravel()

        if np.allclose(opt_deph[0],opt_deph):
            pass
        else:
            raise Exception('All optical dephasing rates are not the same, unknown error')

        ket_size = self.H_eigenvalues[ket_manifold_num].size
        bra_size = self.H_eigenvalues[bra_manifold_num].size

        opt_deph = np.ones((ket_size,bra_size),dtype='complex') * opt_deph[0]

        return np.diag(opt_deph.ravel())

    # def single_vibrational_decoherence_rate2(self,i,j,vibration,manifold_num):
    #     vi = self.H_eigenvectors[manifold_num][:,i]
    #     vj = self.H_eigenvectors[manifold_num][:,j]
    #     rate = 0
    #     for k in range(self.truncation_size):
    #         k_inds = self.vibrational_occupation_to_indices(vibration,k,manifold_num)
    #         kp1_inds = self.vibrational_occupation_to_indices(vibration,k+1,manifold_num)
    #         for k_ind,kp1_ind in zip(k_inds,kp1_inds):
    #             rate = rate + vi[k_ind] * vj[kp1_ind]*np.sqrt(k+1)
    #     return rate

    def set_bath_coupling(self):
        try:
            self.exciton_exciton_dephasing_gamma = self.params['exciton_exciton_dephasing_gamma']
        except KeyError:
            pass

        try:
            self.exciton_decoherence_gamma = self.params['exciton_decoherence_gamma']
        except KeyError:
            pass

        
        try:
            self.site_to_site_decoherence_gamma = self.params['site_to_site_decoherence_gamma']
        except KeyError:
            pass

        try:
            self.site_to_site_dephasing_gamma = self.params['site_to_site_dephasing_gamma']
        except KeyError:
            pass

        
        try:
            self.optical_dephasing_gamma = self.params['optical_dephasing_gamma']
        except KeyError:
            pass

        try:
            self.optical_decoherence_gamma = self.params['optical_decoherence_gamma']
        except KeyError:
            pass

        
        try:
            self.vibrational_gamma = self.params['vibrational_gamma']
        except KeyError:
            self.vibrational_gamma = 0.1

            
        try:
            self.kT = self.params['kT']
        except KeyError:
            pass

    def convert_electronic_instructions_to_full_instructions(self,inst_list):
        new_inst_list = []
        for ins in inst_list:
            left,right = ins
            if self.manifolds_separable == True:
                pass
            else:
                left = self.extract_electronic_subspace(left,0,self.maximum_manifold)
                right = self.extract_electronic_subspace(right,0,self.maximum_manifold)
            left = np.kron(left,self.vibrational_identity)
            right = np.kron(right,self.vibrational_identity)
            new_inst_list.append((left,right))
        return new_inst_list

    def vibronic_manifold_mask(self,manifold_num):
        """Gets the indices of the Hilbert space that occupy a particular electronic
            manifold, including all vibrational degrees of freedom from that manifold
"""
        try:
            vib_size = self.vibrational_mask[0].size
        except AttributeError:
            N = self.truncation_size
            nv = self.num_vibrations
            vib_size = N**nv
        vib_ones = np.ones(vib_size,dtype='int')
        vibronic_occupation_number = np.kron(self.electronic_total_occupation_number,vib_ones)
        manifold_inds = np.where(vibronic_occupation_number == manifold_num)[0]
        return manifold_inds

    def extract_vibronic_coherence(self,O,manifold1,manifold2):
        """Returns result of projecting the Operator O onto manifold1
            on the left and manifold2 on the right
"""
        manifold1_inds = self.vibronic_manifold_mask(manifold1)
        manifold2_inds = self.vibronic_manifold_mask(manifold2)
        O = O[manifold1_inds,:]
        O = O[:,manifold2_inds]
        return O
    
    def extract_vibronic_manifold(self,O,manifold_num):
        """Projects operator into the given electronic excitation manifold
"""
        return self.extract_vibronic_coherence(O,manifold_num,manifold_num)

    def set_L(self):
        self.L = self.make_Liouvillian(self.all_instructions)

    def set_eigensystem(self):
        self.eigfun(self.L)

    def set_L_by_manifold(self,*,H_eigentransform=False,add_eigenstate_decoherence_effects = False):
        all_inst = self.all_instructions
        
        self.L_by_manifold = dict()
        for i in range(self.maximum_manifold+1):
            for j in range(self.maximum_manifold+1):
                key = str(i) + str(j)
                inst = self.extract_coherence_instructions_from_full_instructions(all_inst,i,j,H_eigentransform=H_eigentransform)
                if add_eigenstate_decoherence_effects:
                    inst += self.all_eigenstate_decoherence_instructions_by_coherence(i,j)
                self.L_by_manifold[key] = self.make_Liouvillian(inst)

    def set_eigensystem_by_manifold(self,*,force_detailed_balance = False):
        self.right_eigenvectors_by_manifold = dict()
        self.left_eigenvectors_by_manifold = dict()
        self.eigenvalues_by_manifold = dict()
        for i in range(self.maximum_manifold+1):
            for j in range(self.maximum_manifold+1):
                key = str(i) + str(j)
                if force_detailed_balance:
                    e, r, l = self.eigfun2(i,j,check_eigenvectors = False)
                else:
                    e, r, l = self.eigfun(self.L_by_manifold[key])
                self.right_eigenvectors_by_manifold[key] = r
                self.left_eigenvectors_by_manifold[key] = l
                self.eigenvalues_by_manifold[key] = e

    def make_mu_by_manifold_ket(self,old_manifold,change,*,H_eigentransform=False,L_eigentransform=True):
        i,j = old_manifold
        i2 = i + change
        if i2 >= 0 and i2 <= self.maximum_manifold:
            pass
        else:
            return None, None
        if H_eigentransform:
            Vold = self.H_eigenvectors[i]
            Vnew = self.H_eigenvectors[i2]
        else:
            pass
        j2 = j
        bra_eye = np.eye(self.extract_vibronic_manifold(self.total_hamiltonian,j).shape[0])
        old_key = str(i) + str(j)
        new_key = str(i2) + str(j2)
        all_mus = []
        mu_dtype='float64'
        for pol in self.pols:
            full_mu = self.vibronic_mu_dict[pol]
            mu = self.extract_vibronic_coherence(full_mu,i2,i)
            if H_eigentransform:
                mu = Vnew.T.dot(mu.dot(Vold))
            mu = np.kron(mu,bra_eye)
            if L_eigentransform:
                l = self.left_eigenvectors_by_manifold[new_key]
                r = self.right_eigenvectors_by_manifold[old_key]
                mu = l.dot(mu.dot(r))
            if np.allclose(np.imag(mu),0):
                mu = np.real(mu)
            else:
                mu_dtype = 'complex128'
            all_mus.append(mu)
        mu_shape = all_mus[0].shape
        mu_3d = np.zeros((mu_shape[0],mu_shape[1],3),dtype=mu_dtype)
        for i in range(3):
            mu_3d[:,:,i] = all_mus[i]
        mu_key = old_key + '_to_' + new_key
        return mu_key, mu_3d

    def make_mu_by_manifold_bra(self,old_manifold,change,*,H_eigentransform=False,L_eigentransform=True):
        i,j = old_manifold
        j2 = j + change
        if j2 >= 0 and j2 <= self.maximum_manifold:
            pass
        else:
            return None, None
        if H_eigentransform:
            Vold = self.H_eigenvectors[j]
            Vnew = self.H_eigenvectors[j2]
        else:
            pass
        i2 = i
        ket_eye = np.eye(self.extract_vibronic_manifold(self.total_hamiltonian,i).shape[0])
        old_key = str(i) + str(j)
        new_key = str(i2) + str(j2)
        all_mus = []
        mu_dtype='float64'
        for pol in self.pols:
            full_mu = self.vibronic_mu_dict[pol]
            mu = self.extract_vibronic_coherence(full_mu,j,j2)
            if H_eigentransform:
                mu = Vold.T.dot(mu.dot(Vnew))
            mu = np.kron(ket_eye,mu.T)
            if L_eigentransform:
                l = self.left_eigenvectors_by_manifold[new_key]
                r = self.right_eigenvectors_by_manifold[old_key]
                mu = l.dot(mu.dot(r))
            if np.allclose(np.imag(mu),0):
                mu = np.real(mu)
            else:
                mu_dtype = 'complex128'
            all_mus.append(mu)
        mu_shape = all_mus[0].shape
        mu_3d = np.zeros((mu_shape[0],mu_shape[1],3),dtype=mu_dtype)
        for i in range(3):
            mu_3d[:,:,i] = all_mus[i]
        mu_key = old_key + '_to_' + new_key
        return mu_key, mu_3d

    def append_mu_by_manifold(self,old_manifold,change,ket_flag,H_eigentransform=False,
                              L_eigentransform=True):
        if ket_flag:
            f = self.make_mu_by_manifold_ket
        else:
            f = self.make_mu_by_manifold_bra
        key, mu = f(old_manifold,change,H_eigentransform=H_eigentransform,
                    L_eigentransform=L_eigentransform)
        if key == None:
            pass
        else:
            boolean_mu = np.zeros(mu.shape[:2],dtype='bool')
            boolean_mu[:,:] = np.round(np.sum(np.abs(mu)**2,axis=-1),12)
            mu = mu * boolean_mu[:,:,np.newaxis]
            self.boolean_mu_by_manifold[key] = boolean_mu
            self.mu_by_manifold[key] = mu

    def set_mu_by_manifold(self,H_eigentransform=False,L_eigentransform=True):
        self.mu_by_manifold = dict()
        self.boolean_mu_by_manifold = dict()
        changes = [-1,1]
        for i in range(self.maximum_manifold+1):
            for j in range(self.maximum_manifold+1):
                manifold = (i,j)
                self.append_mu_by_manifold(manifold,1,True,H_eigentransform=H_eigentransform,L_eigentransform=L_eigentransform)
                self.append_mu_by_manifold(manifold,-1,True,H_eigentransform=H_eigentransform,L_eigentransform=L_eigentransform)
                self.append_mu_by_manifold(manifold,1,False,H_eigentransform=H_eigentransform,L_eigentransform=L_eigentransform)
                self.append_mu_by_manifold(manifold,-1,False,H_eigentransform=H_eigentransform,L_eigentransform=L_eigentransform)
                
    def save_mu_by_manifold(self,*,pruned=True):
        if pruned:
            np.savez(os.path.join(self.base_path,'mu_pruned.npz'),**self.mu_by_manifold)
            np.savez(os.path.join(self.base_path,'mu_boolean.npz'),**self.boolean_mu_by_manifold)
        else:
            np.savez(os.path.join(self.base_path,'mu.npz'),**self.mu_by_manifold)

    def save_eigensystem_by_manifold(self):
        np.savez(os.path.join(self.base_path,'eigenvalues.npz'),**self.eigenvalues_by_manifold)
        np.savez(os.path.join(self.base_path,'right_eigenvectors.npz'),**self.right_eigenvectors_by_manifold)
        np.savez(os.path.join(self.base_path,'left_eigenvectors.npz'),**self.left_eigenvectors_by_manifold)
                
    def extract_coherence_instructions_from_full_instructions(self,inst_list,manifold1,manifold2,*,H_eigentransform=False,trim = None):
        new_inst_list = []
        H1 = self.extract_vibronic_manifold(self.total_hamiltonian,manifold1)
        H2 = self.extract_vibronic_manifold(self.total_hamiltonian,manifold2)
        if H_eigentransform:
            V1 = self.H_eigenvectors[manifold1]
            V2 = self.H_eigenvectors[manifold2]
            # E1,V1 = np.linalg.eigh(H1)
            # sort_indices1 = E1.argsort()
            # E1.sort()
            # V1 = V1[:,sort_indices1]
            # for i in range(E1.size):
            #     max_index = np.argmax(np.abs(V1[:,i]))
            #     if np.real(V1[max_index,i]) < 0:
            #         V1[:,i] *= -1
            # E2,V2 = np.linalg.eigh(H2)
            # sort_indices2 = E2.argsort()
            # E2.sort()
            # V2 = V2[:,sort_indices2]
            # for i in range(E2.size):
            #     max_index = np.argmax(np.abs(V2[:,i]))
            #     if np.real(V2[max_index,i]) < 0:
            #         V2[:,i] *= -1
        else:
            V1 = np.eye(H1.shape[0])
            V2 = np.eye(H2.shape[0])
        for (left,right) in inst_list:
            new_left = self.extract_vibronic_manifold(left,manifold1)
            new_left = V1.T.dot(new_left.dot(V1))
            new_right = self.extract_vibronic_manifold(right,manifold2)
            new_right = V2.T.dot(new_right.dot(V2))
            new_inst_list.append((new_left[:trim,:trim],new_right[:trim,:trim]))
        return new_inst_list

    def extract_manifold_instructions_from_full_instructions(self,inst_list,manifold):
        return self.extract_coherence_instructions_from_full_instructions(inst_list,manifold,manifold)
    
    def add_vibrations(self):
        v0 = self.empty_vibrations
        v1 = self.occupied_vibrations
        self.vibrational_hamiltonian = np.zeros(self.total_hamiltonian.shape)
        for i in range(len(v0)):
            self.vibrational_hamiltonian += v0[i]
            self.vibrational_hamiltonian += v1[i]

        self.total_hamiltonian = self.total_hamiltonian + self.vibrational_hamiltonian

    def set_vibrations(self):
        vibration_params = self.params['vibrations']
        # Vibrations in the ground manifold are assumed to be diagonal
        
        
        emp_vibs = [self.construct_vibrational_hamiltonian(mode_dict,0)
                    for mode_dict in vibration_params]
        self.num_vibrations = len(emp_vibs)
        occ_vibs = [self.construct_vibrational_hamiltonian(mode_dict,1)
                    for mode_dict in vibration_params]

        if self.occupation_num_mask:
            self.set_vibrational_total_occupation_number()
        else:
            N = self.truncation_size
            nv = self.num_vibrations
            self.vibrational_mask = (np.arange(N**nv),)
            self.vibrational_identity = np.eye(N**nv)
        empty_vibrations = self.kron_up_vibrations(emp_vibs)
        occupied_vibrations = self.kron_up_vibrations(occ_vibs)

        self.empty_vibrations = []
        self.occupied_vibrations = []
        
        for i in range(self.num_vibrations):
            site_index = vibration_params[i]['site_label']
            if self.manifolds_separable == True:
                empty = self.empty_list[site_index]
                occupied = self.occupied_list[site_index]
            else:
                empty = self.extract_electronic_subspace(self.empty_list[site_index],0,self.maximum_manifold)
                occupied = self.extract_electronic_subspace(self.occupied_list[site_index],0,self.maximum_manifold)
            self.empty_vibrations.append(np.kron(empty,empty_vibrations[i]))
            self.occupied_vibrations.append(np.kron(occupied,occupied_vibrations[i]))

    def kron_up_vibrations(self,vibrations_list):
        n = self.num_vibrations
        if n == 1:
            return vibrations_list
        new_vibrations_list = []
        for i in range(n):
            new_vibration = self.vibration_identity_kron(i,vibrations_list[i])
            if self.occupation_num_mask:
                new_vibration = self.mask_vibrational_space(new_vibration)
            new_vibrations_list.append(new_vibration)
        return new_vibrations_list
            
    def mask_vibrational_space(self,O):
        inds = self.vibrational_mask
        if type(O) is np.ndarray:
            O = O[inds[0],:].copy()
            O = O[:,inds[0]].copy()
            return O
        
        if type(O) is csr_matrix:
            pass
        else:
            O = O.tocsr()
        O = O[inds[0]]
        O = O.transpose()
        O = O[inds[0]]
        O = O.transpose()
        return O

    def vibration_identity_kron(self,position,item):
        """Takes in a single vibrational hamiltonians and krons it with the correct 
            number of vibrational identities, inserting it into its position as indexed by its mode
            position as specified in the input file"""
        identities = [np.eye(self.truncation_size) for n in
                      range(self.num_vibrations-1)]
        identities.insert(position,item)
        mat = identities.pop(0)
        for next_item in identities:
            mat = np.kron(mat,next_item)
        return mat

    def vibrational_vector_of_ones_kron(self,position,item):
        """Takes in a single vibrational hamiltonians and krons it with the correct 
            number of vibrational identities, inserting it into its position as indexed by its mode
            position as specified in the input file"""
        N = self.truncation_size
        nv = self.num_vibrations
        ones_list = [np.ones(N) for i in range(nv-1)]
        ones_list.insert(position,item)
        vec = ones_list.pop(0)
        for next_item in ones_list:
            vec = np.kron(vec,next_item)
        return vec

    def set_vibrational_total_occupation_number(self):
        N = self.truncation_size
        nv = self.num_vibrations
        single_mode_occ = np.arange(N)
        occ_num = self.vibrational_vector_of_ones_kron(0,single_mode_occ)
        for i in range(1,nv):
            occ_num += self.vibrational_vector_of_ones_kron(i,single_mode_occ)
        self.vibrational_total_occupation_number = occ_num
        self.vibrational_mask = np.where(occ_num < N)
        self.vibrational_identity = np.eye(self.vibrational_mask[0].size)

    def construct_vibrational_hamiltonian(self,single_mode,electronic_occupation):
        """For each vibrational mode, construct a list of sparse matrices defining the 
            vibrational hamiltonian for that mode in each excited state"""
        w = single_mode['omega_g']
        lam = single_mode['reorganization'][electronic_occupation]
        d  = single_mode['displacement'][electronic_occupation]
        kin = single_mode['kinetic'][electronic_occupation]
        pot = single_mode['potential'][electronic_occupation]
        aho = AnharmonicDisplaced(self.truncation_size)
        aho.set_ham(lam,d,kin,pot,real=True)
        return 0.5 * w * aho.ham

    # def construct_vibrational_ladder_operator(self,single_mode,electronic_occupation):
    #     """Construct ladder operator given the electronic occupation for that site"""
    #     aho = AnharmonicDisplaced(self.truncation_size)
    #     aho.calculation_size = self.truncation_size
    #     up = aho.create()
    #     ham = self.construct_vibrational_hamiltonian(single_mode,electronic_occupation)
    #     E,V = np.linalg.eigh(ham)
    #     for i in range(E.size):
    #             max_ind = np.argmax(np.abs(V))
    #             if V[max_ind,i] < 0:
    #                 V[:,i] = V[:,i] * -1
    #     up = V.dot(up.dot(V.T))
    #     return up

    def construct_vibrational_ladder_operator(self,single_mode,electronic_occupation):
        """Construct ladder operator given the electronic occupation for that site"""
        w = single_mode['omega_g']
        d  = single_mode['displacement'][electronic_occupation]
        aho = AnharmonicDisplaced(self.truncation_size)
        aho.calculation_size = self.truncation_size
        up = aho.create() - np.eye(self.truncation_size) * d/np.sqrt(2)
        return up

    def set_vibrational_ladder_operators(self):
        vibration_params = self.params['vibrations']
        emp_ups = []
        occ_ups = []

        for i in range(len(vibration_params)):
            ad = self.construct_vibrational_ladder_operator(vibration_params[i],0)
            emp_ups.append(ad)

            ad = self.construct_vibrational_ladder_operator(vibration_params[i],1)
            occ_ups.append(ad)

        empty_ups = self.kron_up_vibrations(emp_ups)
        occupied_ups = self.kron_up_vibrations(occ_ups)
        self.empty_ups = []
        self.occupied_ups = []
        for i in range(self.num_vibrations):
            site_index = vibration_params[i]['site_label']
            if self.manifolds_separable == True:
                empty = self.empty_list[site_index]
                occupied = self.occupied_list[site_index]
            else:
                empty = self.extract_electronic_subspace(self.empty_list[site_index],0,self.maximum_manifold)
                occupied = self.extract_electronic_subspace(self.occupied_list[site_index],0,self.maximum_manifold)
            self.empty_ups.append(np.kron(empty,empty_ups[i]))
            self.occupied_ups.append(np.kron(occupied,occupied_ups[i]))

    def make_vibrational_dissipation_Liouvillian(self):
        ins_list = self.vibrational_dissipation_instructions()
        L = self.make_Liouvillian(ins_list)
        # for i in range(1,len(ins_list)):
        #     L += self.make_Liouvillian(ins_list[i])

        return L

    def vibrational_dissipation_instructions(self):
        gamma = self.vibrational_gamma
        instructions = []
        for k in range(self.num_vibrations):
            E = self.params['vibrations'][k]['omega_g']
            if self.params['vibrations'][k]['potential'][1][0] != 1:
                warnings.warn('The case of different excited and ground state frequencies is not properly handled by thermal dissipation')
            if self.kT == 0:
                N = 0
            else:
                N = 1/(np.exp(E/self.kT)-1)
            O = (self.occupied_ups[k]).T + (self.empty_ups[k]).T
            ins1 = self.make_Lindblad_instructions(gamma*(N+1),O)
            instructions += ins1
            if N == 0:
                pass
            else:
                ins2 = self.make_Lindblad_instructions(gamma*N,O.T)
                instructions += ins2
        return instructions

    # def vibrational_dissipation_instructions(self):
    #     gamma = self.vibrational_gamma
    #     instructions = []
    #     for k in range(self.num_vibrations):
    #         E = self.params['vibrations'][k]['omega_g']
    #         if self.params['vibrations'][k]['potential'][1][0] != 1:
    #             warnings.warn('The case of different excited and ground state frequencies is not properly handled by thermal dissipation')
    #         N = 1/(np.exp(E/self.kT)-1)
    #         O = (self.occupied_ups[k]).T
    #         ins1 = self.make_Lindblad_instructions(gamma*(N+1),O)
    #         ins2 = self.make_Lindblad_instructions(gamma*N,O.T)
    #         instructions += ins1
    #         instructions += ins2

    #         O = (self.empty_ups[k]).T
    #         ins1 = self.make_Lindblad_instructions(gamma*(N+1),O)
    #         ins2 = self.make_Lindblad_instructions(gamma*N,O.T)
    #         instructions += ins1
    #         instructions += ins2
            

    #     return instructions

    def make_total_Liouvillian(self):
        ins = self.make_commutator_instructions(-1j*self.total_hamiltonian)
        self.L = self.make_Liouvillian(ins)
        self.L += self.make_vibrational_dissipation_Liouvillian()

    def make_condon_mu(self):
        try:
            vib_size = self.vibrational_mask[0].size
        except AttributeError:
            N = self.truncation_size
            nv = self.num_vibrations
            vib_size = N**nv
        self.mu = np.kron(self.mu,np.eye(vib_size))
        self.mu_ket_up = np.kron(self.mu_ket_up,np.eye(vib_size))

    def make_condon_mu_dict(self):
        try:
            vib_size = self.vibrational_mask[0].size
        except AttributeError:
            N = self.truncation_size
            nv = self.num_vibrations
            vib_size = N**nv
        self.vibronic_mu_dict = dict()
        for pol in self.pols:
            self.vibronic_mu_dict[pol] =  np.kron(self.mu_dict[pol],np.eye(vib_size))
            
