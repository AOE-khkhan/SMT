"""
Author: Dr. John T. Hwang         <hwangjt@umich.edu>

"""

from __future__ import division

import numpy as np
from scipy.sparse import csc_matrix
from smt.sm import SM

from smt.utils.linear_solvers import get_solver
from smt.utils.caching import _caching_checksum_sm, _caching_load, _caching_save

from smt import RBFlib


class RBF(SM):

    '''
    Radial basis function interpolant with global polynomial trend.
    '''

    def _declare_options(self):
        super(RBF, self)._declare_options()
        declare = self.options.declare

        declare('name', 'RBF', types=str,
                desc='Radial Basis Function interpolant')
        declare('d0', 1.0, types=(int, float, list, np.ndarray),
                desc='basis function scaling parameter in exp(-d^2 / d0^2)')
        declare('poly_degree', -1, values=(-1, 0, 1),
                desc='-1 means no global polynomial, 0 means constant, 1 means linear trend')
        declare('save_solution', False, types=bool,
                desc='Whether to save the linear system solution')
        declare('reg', 1e-10, types=(int, float),
                desc='Regularization coeff.')
        declare('max_print_depth', 5, types=int,
                desc='Maximum depth (level of nesting) to print operation descriptions and times')

    def _fit(self):
        options = self.options

        nx = self.training_pts['exact'][0][0].shape[1]
        if isinstance(options['d0'], (int, float)):
            options['d0'] = [options['d0']] * nx
        options['d0'] = np.atleast_1d(options['d0'])

        num = {}
        # number of inputs and outputs
        num['x'] = self.training_pts['exact'][0][0].shape[1]
        num['y'] = self.training_pts['exact'][0][1].shape[1]
        # number of radial function terms
        num['radial'] = self.training_pts['exact'][0][0].shape[0]
        # number of polynomial terms
        if options['poly_degree'] == -1:
            num['poly'] = 0
        elif options['poly_degree'] == 0:
            num['poly'] = 1
        elif options['poly_degree'] == 1:
            num['poly'] = 1 + num['x']
        num['dof'] = num['radial'] + num['poly']

        self.num = num

        self.printer.max_print_depth = options['max_print_depth']

        xt, yt = self.training_pts['exact'][0]
        jac = RBFlib.compute_jac(0, options['poly_degree'], num['x'], num['radial'],
            num['radial'], num['dof'], options['d0'], xt, xt)

        mtx = np.zeros((num['dof'], num['dof']))
        mtx[:num['radial'], :] = jac
        mtx[:, :num['radial']] = jac.T
        mtx[np.arange(num['radial']), np.arange(num['radial'])] += options['reg']

        rhs = np.zeros((num['dof'], num['y']))
        rhs[:num['radial'], :] = yt

        sol = np.zeros((num['dof'], num['y']))

        solver = get_solver('dense')
        with self.printer._timed_context('Initializing linear solver'):
            solver._initialize(mtx, self.printer)

        for ind_y in range(rhs.shape[1]):
            with self.printer._timed_context('Solving linear system (col. %i)' % ind_y):
                solver._solve(rhs[:, ind_y], sol[:, ind_y], ind_y=ind_y)

        self.sol = sol

    def fit(self):
        """
        Train the model
        """
        checksum = _caching_checksum_sm(self)
        filename = '%s.sm' % self.options['name']

        # If caching (saving) is requested, try to load data
        if self.options['save_solution']:
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
        if self.options['save_solution']:
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
        options = self.options

        xt = self.training_pts['exact'][0][0]
        jac = RBFlib.compute_jac(kx, options['poly_degree'], num['x'], n,
            num['radial'], num['dof'], options['d0'], x, xt)
        return jac.dot(self.sol)
