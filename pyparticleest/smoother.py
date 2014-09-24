""" Rao-Blackwellized Particle smoother implementation """

import numpy
import filter as pf

def bsi_full(pa, model, future_trajs, ut, tt):
    # Perform backward simulation by evaluating all transitation densities
    # and then randomly sample a index from the catagorical distribution

    M = future_trajs.shape[1]
    res = numpy.empty(M, dtype=int)
    for j in xrange(M):
        p_next = model.logp_xnext_full(pa.part, future_trajs[:, j:(j + 1)],
                                       ut, tt)

        w = pa.w + p_next
        w = w - numpy.max(w)
        w_norm = numpy.exp(w)
        w_norm /= numpy.sum(w_norm)
        res[j] = pf.sample(w_norm, 1)
    return res


def bsi_rs(pa, model, future_trajs, ut, tt, maxpdf, max_iter):
    # Perform backward simulation using rejection sampling, after
    # max_iter attemps to find a suitable particle fall back
    # to evaluating all the weights

    M = future_trajs.shape[1]
    todo = numpy.asarray(range(M))
    res = numpy.empty(M, dtype=int)
    weights = numpy.copy(pa.w)
    weights -= numpy.max(weights)
    weights = numpy.exp(weights)
    weights /= numpy.sum(weights)
    for _i in xrange(max_iter):

        ind = numpy.random.permutation(pf.sample(weights, len(todo)))
        pn = model.logp_xnext_full(pa.part[ind], future_trajs[:, todo],
                                   ut=ut, tt=tt)
        test = numpy.log(numpy.random.uniform(size=len(todo)))
        accept = test < pn - maxpdf
        res[todo[accept]] = ind[accept]
        todo = todo[~accept]
        if (len(todo) == 0):
            return res

    # TODO, is there an efficient way to store those weights
    # already calculated to avoid double work, or will that
    # take more time than simply evaulating them all again?
    res[todo] = bsi_full(pa, model, future_trajs[:, todo], ut=ut, tt=tt)
    return res

def bsi_rsas(pa, model, future_trajs, ut, tt, maxpdf, x1, P1, sv, sw, ratio):
    # Perform backward simulation using rejection sampling, adaptively
    # decide when to switch over to the full evaluation of weights by
    # using a Kalman filter to estimate the predicted acceptance rate

    M = future_trajs.shape[1]
    todo = numpy.asarray(range(M))
    res = numpy.empty(M, dtype=int)
    weights = numpy.copy(pa.w)
    weights -= numpy.max(weights)
    weights = numpy.exp(weights)
    weights /= numpy.sum(weights)
    pk = x1
    Pk = P1
    stop_criteria = ratio / len(pa)
    while (True):

        ind = numpy.random.permutation(pf.sample(weights, len(todo)))
        pn = model.logp_xnext_full(pa.part[ind], future_trajs[:, todo],
                                   ut=ut, tt=tt)
        test = numpy.log(numpy.random.uniform(size=len(todo)))
        accept = test < pn - maxpdf
        ak = numpy.sum(accept)
        mk = len(todo)
        res[todo[accept]] = ind[accept]
        todo = todo[~accept]
        if (len(todo) == 0):
            return res
        # meas update for adaptive stop
        mk2 = mk * mk
        sw2 = sw * sw
        pk = pk + (mk * Pk) / (mk2 * Pk + sw2) * (ak - mk * pk)
        Pk = (1 - (mk2 * Pk) / (mk2 * Pk + sw2)) * Pk
        # predict
        pk = (1 - ak / mk) * pk
        Pk = (1 - ak / mk) ** 2 * Pk + sv * sv
        if (pk < stop_criteria):
            break

    res[todo] = bsi_full(pa, model, future_trajs[:, todo], ut=ut, tt=tt)
    return res

def bsi_mcmc(pa, model, future_trajs, ut, tt, R, ancestors):
    # Perform backward simulation using an MCMC sampler proposing new
    # backward particles, initialized with the filtered trajectory

    M = future_trajs.shape[1]
    ind = ancestors
    weights = numpy.copy(pa.w)
    weights -= numpy.max(weights)
    weights = numpy.exp(weights)
    weights /= numpy.sum(weights)
    pind = model.logp_xnext_full(pa.part[ind], future_trajs, ut=ut, tt=tt)
    for _j in xrange(R):
        propind = numpy.random.permutation(pf.sample(weights, M))
        pprop = model.logp_xnext_full(pa.part[propind], future_trajs,
                                      ut=ut, tt=tt)
        diff = pprop - pind
        diff[diff > 0.0] = 0.0
        test = numpy.log(numpy.random.uniform(size=M))
        accept = test < diff
        ind[accept] = propind[accept]
        pind[accept] = pprop[accept]

    return ind

class SmoothTrajectory(object):
    """ Store smoothed trajectory """
    def __init__(self, pt, M=1, method='normal', options=None):
        """ Create smoothed trajectory from filtered trajectory
            pt - particle trajectory to be smoothed  """

        self.traj = None

        T = len(pt)
        self.u = numpy.empty(T, dtype=object)
        self.y = numpy.empty(T, dtype=object)
        self.t = numpy.empty(T, dtype=object)
        for i in xrange(T):
            self.u[i] = pt[i].u
            self.y[i] = pt[i].y
            self.t[i] = pt[i].t

        self.model = pt.pf.model
        if (method == 'full' or method == 'mcmc' or method == 'rs' or method == 'rsas'):
            self.perform_bsi(pt=pt, M=M, method=method, options=options)
        elif (method == 'ancestor'):
            self.perform_ancestors(pt=pt, M=M)
        elif (method == 'mhips'):
            # Initialize using forward trajectories
            self.perform_ancestors(pt=pt, M=M)
            if 'R' in options:
                R = options['R']
            else:
                R = 10
            for _i in xrange(R):
                self.perform_mhips_pass(options=options)

        elif (method == 'mhbp'):
            if 'R' in options:
                R = options['R']
            else:
                R = 10
            self.perform_mhbp(pt=pt, M=M, R=R)
        else:
            raise ValueError('Unknown smoother: %s' % method)

    def __len__(self):
        return len(self.traj)

    def perform_ancestors(self, pt, M):
        """ Create M smoothed trajectories by just using the filtered forward trajectories """
        T = len(pt)

        tmp = numpy.copy(pt[-1].pa.w)
        tmp -= numpy.max(tmp)
        tmp = numpy.exp(tmp)
        tmp = tmp / numpy.sum(tmp)
        ind = pf.sample(tmp, M)
        last_part = self.model.sample_smooth(pt[-1].pa.part[ind],
                                             future_trajs=None,
                                             ut=(pt[-1].u,), yt=(pt[-1].y,),
                                             tt=(pt[-1].t,))

        self.traj = numpy.zeros((T, M, last_part.shape[1]))
        self.traj[-1] = numpy.copy(last_part)

        ancestors = pt[-1].ancestors[ind]

        for t in reversed(xrange(T - 1)):
            step = pt[t]
            pa = step.pa

            ind = ancestors
            ancestors = step.ancestors[ind]
            # Select 'previous' particle
            self.traj[t] = numpy.copy(self.model.sample_smooth(pa.part[ind],
                                                               self.traj[(t + 1):],
                                                               ut=self.u[t:],
                                                               yt=self.y[t:],
                                                               tt=self.t[t:]))

        if hasattr(self.model, 'post_smoothing'):
            # Do e.g. constrained smoothing for RBPS models
            self.traj = self.model.post_smoothing(self)


    def perform_bsi(self, pt, M, method, options):


        # Sample from end time estimates
        tmp = numpy.copy(pt[-1].pa.w)
        tmp -= numpy.max(tmp)
        tmp = numpy.exp(tmp)
        tmp = tmp / numpy.sum(tmp)
        ind = pf.sample(tmp, M)
        last_part = self.model.sample_smooth(pt[-1].pa.part[ind],
                                             future_trajs=None,
                                             ut=self.u[-1:], yt=self.y[-1:],
                                             tt=self.t[-1:])

        self.traj = numpy.zeros((len(pt), M, last_part.shape[1]))
        self.traj[-1] = numpy.copy(last_part)

        if (method == 'full'):
            pass
        elif (method == 'mcmc' or method == 'ancestor' or method == 'mhips'):
            ancestors = pt[-1].ancestors[ind]
        elif (method == 'rs'):
            max_iter = options['R']
        elif (method == 'rsas'):
            x1 = options['x1']
            P1 = options['P1']
            sv = options['sv']
            sw = options['sw']
            ratio = options['ratio']
        else:
            raise ValueError('Unknown sampler: %s' % method)

        for cur_ind in reversed(xrange(len(pt) - 1)):
            step = pt[cur_ind]
            pa = step.pa

            ft = self.traj[(cur_ind + 1):]
            ut = self.u[cur_ind:]
            yt = self.y[cur_ind:]
            tt = self.t[cur_ind:]

            if (method == 'rs'):
                ind = bsi_rs(pa, self.model, ft, ut, tt,
                             options['maxpdf'][cur_ind],
                             int(max_iter))
            elif (method == 'rsas'):
                ind = bsi_rsas(pa, self.model, ft, ut, tt,
                               options['maxpdf'][cur_ind], x1,
                               P1, sv, sw, ratio)
            elif (method == 'mcmc'):
                ind = bsi_mcmc(pa, self.model, ft, ut, tt, options['R'],
                               ancestors)
                ancestors = step.ancestors[ind]
            elif (method == 'full'):
                ind = bsi_full(pa, self.model, ft, ut, tt)
            elif (method == 'ancestor'):
                ind = ancestors
                ancestors = step.ancestors[ind]
            # Select 'previous' particle
            self.traj[cur_ind] = numpy.copy(self.model.sample_smooth(pa.part[ind],
                                                                     ft, ut,
                                                                     yt, tt))

        if hasattr(self.model, 'post_smoothing'):
            # Do e.g. constrained smoothing for RBPS models
            self.traj = self.model.post_smoothing(self)


    def perform_mhbp(self, pt, M, R):

        T = len(pt)

        # Initialise from end time estimates
        tmp = numpy.copy(pt[-1].pa.w)
        tmp -= numpy.max(tmp)
        tmp = numpy.exp(tmp)
        tmp = tmp / numpy.sum(tmp)
        anc = pf.sample(tmp, M)

        last_part = self.model.sample_smooth(pt[T - 1].pa.part[anc],
                                             future_trajs=None,
                                             ut=self.u[(T - 1):],
                                             yt=self.y[(T - 1):],
                                             tt=self.t[(T - 1):])

        self.traj = numpy.zeros((T, M, last_part.shape[1]))

        for t in reversed(xrange(T)):

            # Initialise from filtered estimate
            if (t < T - 1):
                ft = self.traj[(t + 1):]
                self.traj[t] = numpy.copy(self.model.sample_smooth(pt[t].pa.part[anc],
                                                                   ft,
                                                                   ut=self.u[t:],
                                                                   yt=self.y[t:],
                                                                   tt=self.t[t:]))

            else:
                self.traj[t] = numpy.copy(last_part)
                ft = None

            if (t > 0):
                anc = pt[t].ancestors[anc]
                tmp = numpy.copy(pt[t - 1].pa.w)
                tmp -= numpy.max(tmp)
                tmp = numpy.exp(tmp)
                tmp = tmp / numpy.sum(tmp)

            for _ in xrange(R):

                if (t > 0):
                    # Propose new ancestors
                    panc = pf.sample(tmp, M)
                    partp_prop = pt[t - 1].pa.part[panc]
                    partp_curr = pt[t - 1].pa.part[anc]
                    up = self.u[t - 1]
                    tp = self.t[t - 1]
                else:
                    partp_prop = None
                    partp_curr = None
                    up = None
                    tp = None

                (pprop, acc) = mc_step(model=self.model,
                                       partp_prop=partp_prop,
                                       partp_curr=partp_curr,
                                       up=up,
                                       tp=tp,
                                       curpart=self.traj[t, :],
                                       yt=self.y[t:],
                                       ut=self.u[t:],
                                       tt=self.t[t:],
                                       future_trajs=ft)

                # Update with accepted proposals
                self.traj[t, acc] = pprop[acc]
                anc[acc] = panc[acc]

        if hasattr(self.model, 'post_smoothing'):
            # Do e.g. constrained smoothing for RBPS models
            self.traj = self.model.post_smoothing(self)


    def perform_mhips_pass(self, options):
        T = len(self.traj)
        for i in reversed(xrange((T))):
            yt = self.y[i:]
            ut = self.u[i:]
            tt = self.t[i:]

            if (i == T - 1):
                partp = self.traj[i - 1]
                up = self.u[i - 1]
                tp = self.t[i - 1]
                ft = None
            elif (i == 0):
                partp = None
                up = None
                tp = None
                ft = self.traj[(i + 1):]
            else:
                partp = self.traj[i - 1]
                up = self.u[i - 1]
                tp = self.t[i - 1]
                ft = self.traj[(i + 1):]

            (prop, acc) = mc_step(model=self.model, partp_prop=partp,
                                  partp_curr=partp, up=up, tp=tp,
                                  curpart=self.traj[i], yt=yt, ut=ut,
                                  tt=tt, future_trajs=ft)
            self.traj[i, acc] = prop[acc]
            if (i > 0):
                self.traj[i - 1] = self.model.sample_smooth(self.traj[i - 1],
                                                            self.traj[i:],
                                                            ut=self.u[(i - 1):],
                                                            yt=self.y[(i - 1):],
                                                            tt=self.t[(i - 1):])

        if hasattr(self.model, 'post_smoothing'):
                    self.traj = self.model.post_smoothing(self)

    def perform_mhips_pass_reduced(self, pt, M, options):
        """ Runs MHIPS with the proposal density q as p(x_{t+1}|x_t) """
        T = len(self.traj)
        for i in reversed(xrange((T))):

            if (i > 0):
                xprop = numpy.copy(self.traj[i - 1])
                noise = self.model.sample_process_noise(xprop, self.u[i - 1], self.t[i - 1])
                xprop = self.model.update(xprop, self.u[i - 1], self.t[i - 1], noise)
            else:
                xprop = self.model.create_initial_estimate(M)

            if (self.y[i] != None):
                logp_y_prop = self.model.measure(particles=numpy.copy(xprop),
                                                 yt=self.y[i:],
                                                 tt=self.t[i:])

                logp_y_curr = self.model.measure(particles=numpy.copy(self.traj[i]),
                                                 yt=self.y[i:],
                                                 tt=self.t[i:])
            else:
                logp_y_prop = numpy.zeros(M)
                logp_y_curr = numpy.zeros(M)

            if (i < T - 1):
                ft = self.traj[(i + 1):]
                logp_next_prop = self.model.logp_xnext_full(particles=xprop,
                                                            future_trajs=ft,
                                                            ut=self.u[i:],
                                                            tt=self.t[i:])
                logp_next_curr = self.model.logp_xnext_full(particles=self.traj[i],
                                                            future_trajs=ft,
                                                            ut=self.u[i:],
                                                            tt=self.t[i:])
            else:
                ft = None
                logp_next_prop = numpy.zeros(M)
                logp_next_curr = numpy.zeros(M)


            # Calc ratio
            ratio = ((logp_y_prop - logp_y_curr) +
                     (logp_next_prop - logp_next_curr))

            test = numpy.log(numpy.random.uniform(size=M))
            ind = test < ratio
            self.traj[i][ind] = self.model.sample_smooth(xprop[ind], ft,
                                                         ut=self.u[i:],
                                                         yt=self.y[i:],
                                                         tt=self.t[i:])
            if (i > 0):
                self.traj[i - 1] = self.model.sample_smooth(self.traj[i - 1],
                                                            self.traj[i:],
                                                            ut=self.u[(i - 1):],
                                                            yt=self.y[(i - 1):],
                                                            tt=self.t[(i - 1):])


def mc_step(model, partp_prop, partp_curr, up, tp, curpart,
            yt, ut, tt, future_trajs):
    " Perform a single iteration of the MCMC sampler used for MHIPS and MHBP"
    M = len(curpart)

    xprop = model.propose_smooth(partp=partp_prop,
                                 up=up,
                                 tp=tp,
                                 yt=yt,
                                 ut=ut,
                                 tt=tt,
                                 future_trajs=future_trajs)

    # Accept/reject new sample
    logp_q_prop = model.logp_smooth(xprop,
                                    partp=partp_prop,
                                    up=up,
                                    tp=tp,
                                    yt=yt,
                                    ut=ut,
                                    tt=tt,
                                    future_trajs=future_trajs)
    logp_q_curr = model.logp_smooth(curpart,
                                    partp=partp_curr,
                                    up=up,
                                    tp=tp,
                                    yt=yt,
                                    ut=ut,
                                    tt=tt,
                                    future_trajs=future_trajs)


    if (partp_prop != None and partp_curr != None):
        logp_prev_prop = model.logp_xnext(particles=partp_prop,
                                        next_part=xprop,
                                        u=up,
                                        t=tp)
        logp_prev_curr = model.logp_xnext(particles=partp_curr,
                                        next_part=curpart,
                                        u=up,
                                        t=tp)
    else:
        logp_prev_prop = numpy.zeros(M)
        logp_prev_curr = numpy.zeros(M)

    if (yt != None and yt[0] != None):
        logp_y_prop = model.measure(particles=numpy.copy(xprop),
                                    y=yt[0], t=tt[0])

        logp_y_curr = model.measure(particles=numpy.copy(curpart),
                                    y=yt[0], t=tt[0])
    else:
        logp_y_prop = numpy.zeros(M)
        logp_y_curr = numpy.zeros(M)

    if (future_trajs != None):
        logp_next_prop = model.logp_xnext_full(particles=xprop,
                                               future_trajs=future_trajs,
                                               ut=ut,
                                               tt=tt)
        logp_next_curr = model.logp_xnext_full(particles=curpart,
                                               future_trajs=future_trajs,
                                               ut=ut,
                                               tt=tt)
    else:
        logp_next_prop = numpy.zeros(M)
        logp_next_curr = numpy.zeros(M)


    # Calc ratio
    ratio = ((logp_prev_prop - logp_prev_curr) +
             (logp_y_prop - logp_y_curr) +
             (logp_next_prop - logp_next_curr) +
             (logp_q_curr - logp_q_prop))

    test = numpy.log(numpy.random.uniform(size=M))
    acc = test < ratio
    return (xprop, acc)
