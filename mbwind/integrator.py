# -*- coding: utf-8 -*-
"""
Created on Wed Feb 22 17:24:13 2012

@author: Rick Lupton
"""

from __future__ import division
import time
import sys
import numpy as np
from numpy import zeros, dot
from scipy.integrate import ode


##################
###  Outputs   ###
##################

class StateOutput(object):
    def __init__(self, state_name, deriv=0, local=False, label=None):
        if not deriv in (0, 1, 2):
            raise ValueError('deriv must be 0, 1 or 2')
        if label is None:
            label = "state <{}>".format(state_name)
            if deriv == 1:
                label = "d/dt " + label
            if deriv == 2:
                label = "d2/dt2 " + label
            if local:
                label += " [local]"
        self.state_name = state_name
        self.deriv = deriv
        self.local = local
        self.label = label

    def __str__(self):  # pragma: no cover
        return str(self.label)

    def __call__(self, system):
        if self.deriv == 0:
            q = system.q
        elif self.deriv == 1:
            q = system.qd
        elif self.deriv == 2:
            q = system.qdd
        output = q[self.state_name]

        if self.local:
            if not q.get_type(self.state_name) in ('node', 'ground'):
                raise RuntimeError(
                    'Only nodes can be transformed to local coordinates')
            R = system.q[self.state_name][3:].reshape((3, 3))
            v = dot(R.T, output[:3])
            if self.deriv == 0:
                assert len(output) == 12
                assert np.allclose(dot(R.T, output[3:].reshape((3, 3))),
                                   np.eye(3))
                output = np.r_[v, np.eye(3).flatten()]
            else:
                assert len(output) == 6
                w = dot(R.T, output[3:])
                output = np.r_[v, w]

        return output


class LoadOutput(object):
    def __init__(self, state_name, local=False, label=None):
        if label is None:
            label = "reaction load on <{}>{}".format(
                state_name, " [local]" if local else "")
        self.state_name = state_name
        self.local = local
        self.label = label

    def __str__(self):  # pragma: no cover
        return str(self.label)

    def __call__(self, system):
        assert (system.joint_reactions.get_type(self.state_name)
                in ('node', 'ground'))
        output = system.joint_reactions[self.state_name]

        if self.local:
            assert len(output) == 6
            R = system.q[self.state_name][3:].reshape((3, 3))
            v = dot(R.T, output[:3])
            w = dot(R.T, output[3:])
            output = np.r_[v, w]

        return output


class CustomOutput(object):
    def __init__(self, func, label=None):
        if label is None:
            label = "custom output {}".format(func)
        self.func = func
        self.label = label

    def __str__(self):  # pragma: no cover
        return str(self.label)

    def __call__(self, system):
        return np.atleast_1d(self.func(system))


class Integrator(object):
    """
    Solve and integrate a system, keeping track of which outputs are required.
    """
    def __init__(self, system, outputs=('pos',), method='dopri5'):
        self.system = system
        self.t = np.zeros(0)
        self.y = []
        self.method = method

        # By default, output DOFs
        self._outputs = []
        idof = self.system.qd.names_by_type('strain')
        if 'pos' in outputs:
            for i in idof:
                self.add_output(StateOutput(i, deriv=0))
        if 'vel' in outputs:
            for i in idof:
                self.add_output(StateOutput(i, deriv=1))
        if 'acc' in outputs:
            for i in idof:
                self.add_output(StateOutput(i, deriv=2))

    def add_output(self, output):
        self._outputs.append(output)

    def outputs(self):
        return [output(self.system) for output in self._outputs]

    @property
    def labels(self):  # pragma: no cover
        return [str(output) for output in self._outputs]

    def integrate(self, tvals, dt=None, t0=0.0, callback=None,
                  extra_states=None, nprint=20):

        if not np.iterable(tvals):
            assert dt is not None and t0 is not None
            tvals = np.arange(0.0, tvals, dt)
        out_tvals = tvals[tvals >= t0]
        it0 = np.nonzero(tvals >= t0)[0][0]

        if extra_states is None:
            extra_states = zeros(0)

        # prepare for first outputs
        #self.system.update_kinematics(0.0) # work out kinematics
        #self.system.solve_reactions()

        self.t = out_tvals
        self.y = []
        for y0 in self.outputs():
            self.y.append(zeros((len(out_tvals),) + y0.shape))
        if len(extra_states) > 0:
            self.y.append(zeros((len(out_tvals), len(extra_states))))

        iDOF_q = self.system.q.indices_by_type('strain')
        iDOF_qd = self.system.qd.indices_by_type('strain')
        assert len(iDOF_q) == len(iDOF_qd)
        nStruct = len(iDOF_q)
        nOther = len(extra_states)

        # Gradient function
        def _func(ti, yi):
            # update system state with states and state rates
            # y constains [other, strains, d(strains)/dt]
            q_other = yi[:nOther]
            self.system.q[iDOF_q] = yi[nOther:nOther+nStruct]
            self.system.qd[iDOF_qd] = yi[nOther+nStruct:]
            self.system.time = ti
            self.system.update_kinematics()

            # Callback may e.g. set element loading
            if callback:
                q_struct = self.system.get_state()
                qd_other = callback(self.system, ti, q_struct, q_other)
                assert len(qd_other) == nOther
            else:
                qd_other = []

            # solve system
            self.system.update_matrices()
            self.system.solve_accelerations()

            # new state vector is [other_dot, strains_dot, strains_dotdot]
            return np.r_[
                qd_other,
                self.system.qd[iDOF_qd],
                self.system.qdd[iDOF_qd]
            ]

        # Initial conditions
        z0 = np.r_[
            extra_states,
            self.system.q[iDOF_q],
            self.system.qd[iDOF_qd]
        ]

        # Setup actual integrator
        integrator = ode(_func)
        integrator.set_integrator(self.method)
        integrator.set_initial_value(z0, 0.0)

        if nprint is not None:  # pragma: no cover
            print('Running simulation:',)
            sys.stdout.flush()
            tstart = time.clock()

            def print_status(it, t):
                if (it % nprint) == 0:
                    sys.stdout.write('.' if (t >= t0) else '-')
                    sys.stdout.flush()
        else:  # pragma: no cover
            def print_status(it, t):
                pass

        # Update for first outputs
        _func(0.0, z0)

        try:
            for it, t in enumerate(tvals):
                if t > 0:
                    integrator.integrate(t)
                    if not integrator.successful():  # pragma: no cover
                        print('stopping')
                        break

                # Wrap joint angles etc
                self.system.q.wrap_states()

                if t >= t0:
                    # Update nodal accelerations from DOFs' accelerations
                    self.system.update_kinematics()

                    # Calculate reaction forces (backwards iteration down tree)
                    self.system.solve_reactions()

                    # Save outputs
                    for y, out in zip(self.y, self.outputs()):
                        y[it - it0] = out
                    if len(extra_states) > 0:
                        self.y[-1][it - it0] = integrator.y[:nOther]

                print_status(it, t)

        except KeyboardInterrupt:  # pragma: no cover
            if nprint is not None:
                print('stopping')
        else:
            if nprint is not None:
                print('done')

        if nprint is not None:
            elapsed_time = time.clock() - tstart
            print(' (%.1f seconds, %d%% of simulation time)' %
                  (elapsed_time, 100*elapsed_time/tvals[-1]))

        return self.t, self.y
