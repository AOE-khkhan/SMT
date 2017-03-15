"""
Author: Dr. John T. Hwang <hwangjt@umich.edu>
"""
from __future__ import division

import numpy as np
import scipy.sparse
from six.moves import range

from smt.utils.linear_solvers import get_solver
from smt.utils.line_search import get_line_search_class
from smt.utils.caching import _caching_checksum_sm, _caching_load, _caching_save
from smt.sm import SM

from smt import RMTSlib


class RMT(SM):
    """
    Regularized Minimal-energy Tensor-product interpolant base class for RMTS and RMTB.
    """

    def _set_default_options(self):
        sm_options = {
            'xlimits': [],    # flt ndarray[nx, 2]: lower/upper bounds in each dimension
            'smoothness': [], # flt ndarray[nx]: smoothness parameter in each dimension
            'reg_dv': 1e-10, # regularization coeff. for dv block
            'reg_cons': 1e-10, # negative of reg. coeff. for Lagrange mult. block
            'extrapolate': False, # perform linear extrapolation for external eval points
            'min_energy': True, # whether to include energy minimizaton terms
            'approx_norm': 4, # order of norm in least-squares approximation term
            'use_mtx_free': False, # whether to solve the linear system in a matrix-free way
            'solver': 'krylov',    # Linear solver: 'gmres' or 'cg'
            'max_nln_iter': 10, # number of nonlinear iterations
            'line_search': 'backtracking', # line search algorithm
            'mg_factors': [], # Multigrid level
            'save_solution': False,  # Whether to save linear system solution
            'max_print_depth': 100, # Maximum depth (level of nesting) to print
        }
        printf_options = {
            'global': True,     # Overriding option to print output
            'time_eval': True,  # Print evaluation times
            'time_train': True, # Print assembly and solution time summary
            'problem': True,    # Print problem information
            'solver': True,     # Print convergence progress (i.e., residual norms)
        }

        self.sm_options = sm_options
        self.printf_options = printf_options

    def _initialize_hessian(self):
        diag = self.sm_options['reg_dv'] * np.ones(self.num['dof'])
        arange = np.arange(self.num['dof'])
        full_hess = scipy.sparse.csc_matrix((diag, (arange, arange)))
        return full_hess

    def _compute_approx_terms(self):
        # This computates the approximation terms for the training points.
        # We loop over kx: 0 is for values and kx>0 represents
        # the 1-based index of the derivative given by the training point data.
        num = self.num
        sm_options = self.sm_options
        xlimits = sm_options['xlimits']

        full_jac_dict = {}
        for kx in self.training_pts['exact']:
            xt, yt = self.training_pts['exact'][kx]

            xmin = np.min(xt, axis=0)
            xmax = np.max(xt, axis=0)
            assert np.all(xlimits[:, 0] <= xmin), 'Training pts below min for %s' % kx
            assert np.all(xlimits[:, 1] >= xmax), 'Training pts above max for %s' % kx

            data, rows, cols = self._compute_jac(kx, 0, xt)

            nt = xt.shape[0]
            full_jac = scipy.sparse.csc_matrix((data, (rows, cols)), shape=(nt, num['coeff']))

            full_jac_dict[kx] = (full_jac, full_jac.T.tocsc())

        return full_jac_dict

    def _opt_func(self, sol, p, full_hess, full_jac_dict, yt_dict):
        func = 0.5 * np.dot(sol, full_hess * sol)
        for kx in self.training_pts['exact']:
            full_jac, full_jac_T = full_jac_dict[kx]
            yt = yt_dict[kx]
            func += 0.5 * np.sum((full_jac * sol - yt) ** p)

        return func

    def _opt_grad(self, sol, p, full_hess, full_jac_dict, yt_dict):
        grad = full_hess * sol
        for kx in self.training_pts['exact']:
            full_jac, full_jac_T = full_jac_dict[kx]
            yt = yt_dict[kx]
            grad += 0.5 * full_jac_T * p * (full_jac * sol - yt) ** (p - 1)

        return grad

    def _opt_hess(self, sol, p, full_hess, full_jac_dict, yt_dict):
        if self.sm_options['use_mtx_free']:
            return self._opt_hess_op(sol, p, full_hess, full_jac_dict, yt_dict)
        else:
            return self._opt_hess_mtx(sol, p, full_hess, full_jac_dict, yt_dict)

    def _opt_hess_mtx(self, sol, p, full_hess, full_jac_dict, yt_dict):
        hess = scipy.sparse.csc_matrix(full_hess)
        for kx in self.training_pts['exact']:
            full_jac, full_jac_T = full_jac_dict[kx]
            yt = yt_dict[kx]

            diag_vec = p * (p - 1) * (full_jac * sol - yt) ** (p - 2)
            diag_mtx = scipy.sparse.diags(diag_vec, format='csc')
            hess += 0.5 * full_jac_T * diag_mtx * full_jac

        return hess

    def _opt_hess_op(self, sol, p, full_hess, full_jac_dict, yt_dict):
        class SpMatrix(object):
            def __init__(self):
                self.shape = full_hess.shape
                self.diag_vec = {}
                for kx in full_jac_dict:
                    full_jac, full_jac_T = full_jac_dict[kx]
                    yt = yt_dict[kx]
                    self.diag_vec[kx] = p * (p - 1) * (full_jac * sol - yt) ** (p - 2)
            def dot(self, other):
                vec = full_hess * other
                for kx in full_jac_dict:
                    full_jac, full_jac_T = full_jac_dict[kx]
                    vec += 0.5 * full_jac_T * (self.diag_vec[kx] * (full_jac * other))
                return vec

        mtx = SpMatrix()
        op = scipy.sparse.linalg.LinearOperator(mtx.shape, matvec=mtx.dot)
        return op

    def _opt_hess_2(self, full_hess, full_jac_dict):
        if self.sm_options['use_mtx_free']:
            return self._opt_hess_op_2(full_hess, full_jac_dict)
        else:
            return self._opt_hess_mtx_2(full_hess, full_jac_dict)

    def _opt_hess_mtx_2(self, full_hess, full_jac_dict):
        p = 2

        hess = scipy.sparse.csc_matrix(full_hess)
        for kx in self.training_pts['exact']:
            full_jac, full_jac_T = full_jac_dict[kx]
            hess += 0.5 * p * (p - 1) * full_jac_T * full_jac

        return hess

    def _opt_hess_op_2(self, full_hess, full_jac_dict):
        p = 2

        class SpMatrix(object):
            def __init__(self):
                self.shape = full_hess.shape
            def dot(self, other):
                vec = full_hess * other
                for kx in full_jac_dict:
                    full_jac, full_jac_T = full_jac_dict[kx]
                    vec += 0.5 * p * (p - 1) * full_jac_T * (full_jac * other)
                return vec

        mtx = SpMatrix()
        op = scipy.sparse.linalg.LinearOperator(mtx.shape, matvec=mtx.dot)
        return op

    def _opt_norm(self, sol, p, full_hess, full_jac_dict, yt_dict):
        grad = self._opt_grad(sol, p, full_hess, full_jac_dict, yt_dict)
        return np.linalg.norm(grad)

    def _get_yt_dict(self, ind_y):
        yt_dict = {}
        for kx in self.training_pts['exact']:
            xt, yt = self.training_pts['exact'][kx]
            yt_dict[kx] = yt[:, ind_y]
        return yt_dict

    def _solve(self, full_hess, full_jac_dict, mg_matrices):
        num = self.num
        sm_options = self.sm_options

        solver = get_solver(sm_options['solver'])
        ls_class = get_line_search_class(sm_options['line_search'])

        total_size = int(num['dof'])
        rhs = np.zeros((total_size, num['y']))
        sol = np.zeros((total_size, num['y']))
        d_sol = np.zeros((total_size, num['y']))

        with self.printer._timed_context('Solving initial linear problem (n=%i)' % total_size):

            with self.printer._timed_context('Assembling linear system'):
                mtx = self._opt_hess_2(full_hess, full_jac_dict)
                for ind_y in range(num['y']):
                    yt_dict = self._get_yt_dict(ind_y)
                    rhs[:, ind_y] = -self._opt_grad(sol[:, ind_y], 2, full_hess,
                                                    full_jac_dict, yt_dict)

            with self.printer._timed_context('Initializing linear solver'):
                solver._initialize(mtx, self.printer, mg_matrices=mg_matrices)

            for ind_y in range(rhs.shape[1]):
                with self.printer._timed_context('Solving linear system (col. %i)' % ind_y):
                    solver._solve(rhs[:, ind_y], sol[:, ind_y], ind_y=ind_y)

        p = self.sm_options['approx_norm']
        for ind_y in range(rhs.shape[1]):

            with self.printer._timed_context('Solving nonlinear problem (col. %i)' % ind_y):

                yt_dict = self._get_yt_dict(ind_y)

                if sm_options['max_nln_iter'] > 0:
                    norm = self._opt_norm(sol[:, ind_y], p, full_hess, full_jac_dict, yt_dict)
                    fval = self._opt_func(sol[:, ind_y], p, full_hess, full_jac_dict, yt_dict)
                    self.printer(
                        'Nonlinear (itn, iy, grad. norm, func.) : %3i %3i %15.9e %15.9e'
                        % (0, ind_y, norm, fval))

                for nln_iter in range(sm_options['max_nln_iter']):
                    with self.printer._timed_context():
                        with self.printer._timed_context('Assembling linear system'):
                            mtx = self._opt_hess(sol[:, ind_y], p, full_hess,
                                                    full_jac_dict, yt_dict)
                            rhs[:, ind_y] = -self._opt_grad(sol[:, ind_y], p, full_hess,
                                                            full_jac_dict, yt_dict)

                        with self.printer._timed_context('Initializing linear solver'):
                            solver._initialize(mtx, self.printer, mg_matrices=mg_matrices)

                        with self.printer._timed_context('Solving linear system'):
                            solver._solve(rhs[:, ind_y], d_sol[:, ind_y], ind_y=ind_y)

                        func = lambda x: self._opt_func(x, p, full_hess,
                                                        full_jac_dict, yt_dict)
                        grad = lambda x: self._opt_grad(x, p, full_hess,
                                                        full_jac_dict, yt_dict)

                        ls = ls_class(sol[:, ind_y], d_sol[:, ind_y], func, grad)
                        with self.printer._timed_context('Performing line search'):
                            sol[:, ind_y] = ls(1.0)

                    norm = self._opt_norm(sol[:, ind_y], p, full_hess,
                                          full_jac_dict, yt_dict)
                    fval = self._opt_func(sol[:, ind_y], p, full_hess,
                                          full_jac_dict, yt_dict)
                    self.printer(
                        'Nonlinear (itn, iy, grad. norm, func.) : %3i %3i %15.9e %15.9e'
                        % (nln_iter + 1, ind_y, norm, fval))

                    if norm < 1e-16:
                        break

        return sol

    def fit(self):
        """
        Train the model
        """
        checksum = _caching_checksum_sm(self)
        filename = '%s.sm' % self.sm_options['name']

        # If caching (saving) is requested, try to load data
        if self.sm_options['save_solution']:
            loaded, data = _caching_load(filename, checksum)
        else:
            loaded = False

        # If caching not requested or loading failed, actually run
        if not loaded:
            self._fit()
        else:
            self.sol = data['sol']
            self.num = data['num']

        # If caching (saving) is requested, save data
        if self.sm_options['save_solution']:
            data = {'sol': self.sol, 'num': self.num}
            _caching_save(filename, checksum, data)

    def evaluate(self, x, kx):
        """
        Evaluate the surrogate model at x.

        Parameters
        ----------
        x : np.ndarray[n_eval,dim]
            An array giving the point(s) at which the prediction(s) should be made.
        kx : int or None
            None if evaluation of the interpolant is desired.
            int  if evaluation of derivatives of the interpolant is desired
                 with respect to the kx^{th} input variable (kx is 0-based).

        Returns
        -------
        y : np.ndarray[n_eval,1]
            - An array with the output values at x.
        """
        n = x.shape[0]

        num = self.num
        sm_options = self.sm_options

        data, rows, cols = self._compute_jac(kx, 0, x)

        # In the explanation below, n is the number of dimensions, and
        # a_k and b_k are the lower and upper bounds for x_k.
        #
        # A C1 extrapolation can get very tricky, so we implement a simple C0
        # extrapolation. We basically linarly extrapolate from the nearest
        # domain point. For example, if n = 4 and x2 > b2 and x3 > b3:
        #    f(x1,x2,x3,x4) = f(x1,b2,b3,x4) + dfdx2 (x2-b2) + dfdx3 (x3-b3)
        #    where the derivatives are evaluated at x1,b2,b3,x4 (called b) and
        #    dfdx1|x = dfdx1|b + d2fdx1dx2|b (x2-b2) + d2fdx1dx3|b (x3-b3)
        #    dfdx2|x = dfdx2|b.
        # The dfdx2|x derivative is what it is because f and all derivatives
        # evaluated at x1,b2,b3,x4 are constant with respect to changes in x2.
        # On the other hand, the dfdx1|x derivative is what it is because
        # f and all derivatives evaluated at x1,b2,b3,x4 change with x1.
        # The extrapolation function is non-differentiable at boundaries:
        # i.e., where x_k = a_k or x_k = b_k for at least one k.
        if sm_options['extrapolate']:

            # First we evaluate the vector pointing to each evaluation points
            # from the nearest point on the domain, in a matrix called dx.
            # If the ith evaluation point is not external, dx[i, :] = 0.
            ndx = n * num['support']
            dx = RMTSlib.compute_ext_dist(num['x'], n, ndx, sm_options['xlimits'], x)
            isexternal = np.array(np.array(dx, bool), float)

            for ix in range(num['x']):
                # Now we compute the first order term where we have a
                # derivative times (x_k - b_k) or (x_k - a_k).
                data_tmp, rows, cols = self._compute_jac(kx, ix+1, x)
                data_tmp *= dx[:, ix]

                # If we are evaluating a derivative (with index kx),
                # we zero the first order terms for which dx_k = 0.
                if kx != 0:
                    data_tmp *= 1 - isexternal[:, kx-1]

                data += data_tmp

        mtx = scipy.sparse.csc_matrix((data, (rows, cols)), shape=(n, num['coeff']))

        return mtx.dot(self.sol)
