# @Author: Andrés Gúrpide <agurpide>
# @Date:   05-02-2022
# @Email:  agurpidelash@irap.omp.eu
# @Last modified by:   agurpide
# @Last modified time: 28-05-2024
import numpy as np
from mind_the_gaps.lightcurves import GappyLightcurve
from celerite.modeling import ConstantModel
from mind_the_gaps.models.mean_models import LinearModel, GaussianModel, SineModel
import emcee
import celerite
from multiprocessing import Pool
from functools import partial
from mind_the_gaps.stats import neg_log_like
from scipy.optimize import minimize
import warnings

class GPModelling:
    """
    The interface for Gaussian Process (GP) modeling. Currently uses celerite.
    """
    meanmodels = ["linear", "constant", "gaussian"]

    def __init__(self, lightcurve, kernel, mean_model: str =None):
        """

        Parameters
        ----------
        lightcurve: mind_the_gaps.GappyLightcurve,
            An instance of a lightcurve
        model:celerite.terms.Term,
            The model to be fitted to the lightcurve
        mean_model: str
            Mean model. If given it will be fitted, otherwise assumed the mean value.
            Available implementations are Constant, Linear and Gaussian.
        """
        self._lightcurve = lightcurve

        meanmodel, fit_mean = self._build_mean_model(mean_model)

        self.gp = celerite.GP(kernel, mean=meanmodel, fit_mean=fit_mean)
        # self.gp = GaussianProcess(kernel, self._lightcurve.times, diag=self._lightcurve.dy**2) --> Tiny GP
        # initialize GP ( # You always need to call compute once.)
        self.gp.compute(self._lightcurve.times, self._lightcurve.dy + 1e-12)
        self.initial_params = self.gp.get_parameter_vector()
        self._ndim = len(self.initial_params)
        self._autocorr = []
        self._loglikelihoods = None
        self._mcmc_samples = None


    def _build_mean_model(self, meanmodel):
        """Construct the GP mean model based on lightcurve properties and
        input string

        Parameters
        ----------
        meanmodel:str

        Returns a celerite.modeling and a bool indicating whether to the mean model is fitted or not
        """
        maxy = np.max(self._lightcurve.y)

        if meanmodel is None:
            # no fitting case
            meanmodel = ConstantModel(self._lightcurve.mean,
                        bounds=[(np.min(self._lightcurve.y), maxy)])
            return meanmodel, False

        elif meanmodel.lower() not in GPModelling.meanmodels:
            raise ValueError("Input mean model %s not implemented! Only \n %s \n are available" % (meanmodel, "\t".join(GPModelling.meanmodels)))

        elif meanmodel.lower()=="constant":
            meanlabels = ["$\mu$"]

        elif meanmodel.lower()=="linear":
            slope_guess = np.sign(self._lightcurve.y[-1] - self._lightcurve.y[0])
            minindex = np.argmin(self._lightcurve.times)
            maxindex = np.argmax(self._lightcurve.times)
            slope_bound = (self._lightcurve.y[maxindex] - self._lightcurve.y[minindex]) / (self._lightcurve.times[maxindex] - self._lightcurve.times[minindex])
            if slope_guess > 0 :
                min_slope = slope_bound
                max_slope = -slope_bound
            else:
                min_slope = -slope_bound
                max_slope = slope_bound
            slope = np.cov(self._lightcurve.times, self._lightcurve.y)[0, 1] / np.var(self._lightcurve.times)
            meanmodel = LinearModel(0, 1.5,
                                    bounds=[(-np.inf, np.inf), (-np.inf, np.inf)])
            meanlabels = ["$m$", "$b$"]

        elif meanmodel.lower()=="gaussian":
            sigma_guess = (self._lightcurve.duration) / 2
            amplitude_guess = (maxy - np.min(y)) * np.sqrt(2 * np.pi)* sigma_guess
            mean_guess = self._lightcurve.times[len(self._lightcurve.times)//2]
            meanmodel = GaussianModel(mean_guess, sigma_guess, amplitude_guess,
                                      bounds=[(self._lightcurve.times[0], self._lightcurve.times[-1]), (0, self._lightcurve.duration),
                                      (maxy * np.sqrt(2 * np.pi) * self._lightcurve.duration, 50 * maxy * np.sqrt(2 * np.pi) * self._lightcurve.duration)])

            meanlabels = ["$\mu$", "$\sigma$", "$A$"]

        return meanmodel, True


    def _log_probability(self, params):
        """Logarithm of the posteriors of the Gaussian process

        Parameters
        ----------
        params: list
            List of parameters of the GP at which to calculate the posteriors
        y: array_like
            The dataset (count rates, lightcurves, etc)
        gp: celerite.GP
            An instance of the Gaussian Process solver

        Returns the log of the posterior (float)
        https://celerite.readthedocs.io/en/stable/tutorials/modeling/"""

        self.gp.set_parameter_vector(params)

        lp = self.gp.log_prior()
        if not np.isfinite(lp):
            return -np.inf
        return lp + self.gp.log_likelihood(self._lightcurve.y)


    def _neg_log_like(self, params):
        self.gp.set_parameter_vector(params)
        return -self.gp.log_likelihood(self._lightcurve.y)


    def fit(self, initial_params=None):
        """Fit the GP by running a minimization rutine

        initial_params:array_like
            Set of initial paramteres. If not given uses those given at initialization
        Returns the output of scipy.minimize
        """
        if initial_params is None:
            initial_params = self.initial_params

        solution = minimize(self._neg_log_like, initial_params, method="L-BFGS-B",
                            bounds=self.gp.get_parameter_bounds())
        return solution


    def derive_posteriors(self, initial_chain_params=None, fit=True, converge=True, max_steps=10000,
                        walkers=12, cores=6, progress=True):
        """Derive GP Posteriors

        Parameters
        ----------
        initial_params: array_like,
            Set of initial parameters for the fit. If not given will use those given at initialization
        fit: bool,
            Whether to run a minimization routine prior to running the MCMC chains
        converge: bool,
            Whether to stop the chains if convergence is detected
        max_steps: int,
            Maximum number of steps for the chains
        walkers: int,
            Number of walkers for the chains
        cores: int,
            Number of cores for parallelization
        """
        # set the initial parameters if not given
        if initial_chain_params is None:
            if not fit:
                initial_params = self.initial_params

            else:
                solution = self.fit(self.initial_params)
                initial_params = solution.x
        
            initial_chain_params = self.spread_walkers(walkers, initial_params, np.array(self.gp.get_parameter_bounds()))
        
        every_samples = 500
        # This will be useful to testing convergence
        old_tau = np.inf
        # parallelize only if cores > 1
        pool =  Pool(cores) if cores >1 else None   
        self.converged = False
        sampler = emcee.EnsembleSampler(walkers, self._ndim, self._log_probability, pool=pool)
        for sample in sampler.sample(initial_chain_params, iterations=max_steps, progress=progress):
            # Only check convergence every 100 steps
            if sampler.iteration % every_samples:
                continue

            # Compute the autocorrelation time so far
            # Using tol=0 means that we'll always get an estimate even
            # if it isn't trustworthy
            tau = sampler.get_autocorr_time(tol=0)
            self.autocorr.append(np.mean(tau))

            # Check convergence
            if np.all(tau * 100 < sampler.iteration) and np.all(np.abs(old_tau - tau) / tau < 0.01) and converge:
                print("Convergence reached after %d samples!" % sampler.iteration)
                self.converged = True
                break
            old_tau = tau
        if pool is not None:
            pool.close()
            pool.join() # Ensures all worker processes are cleaned up

        self._tau = tau
        mean_tau = np.mean(tau)

        if not self.converged:
            warnings.warn(f"The chains did not converge after {sampler.iteration} iterations!")
            # tau will be very large here, so let's reduce the numbers
            thin = int(mean_tau / 4)
            discard = int(mean_tau) * 5 # avoid blowing up if discard is larger than the number of samples, this happens if the fit has not converged

        else:
            discard = int(mean_tau * 40)
            if discard > max_steps:
                discard = int(mean_tau * 10)
            thin = int(mean_tau / 2)

        self._loglikelihoods = sampler.get_log_prob(discard=discard, thin=thin, flat=True)
        self._mcmc_samples = sampler.get_chain(discard=discard, thin=thin, flat=True)
        self._sampler = sampler


    def spread_walkers(self, walkers, parameters, bounds, percent=0.1):
        """Spread the walkers using a Gaussian distribution around a given set of parameters
        walkers:int,
            Number of walkers
        parameters:array_like
            Parameters around which the chains need to be spread
        bounds: array of tuples
            Bounds (min, max) for each of the parameters (in same order)
        percent: float,
            By which percentage factor (0-1) scale the initial parameters for the standard deviation
                of the Gaussians. Default: 0.1 (i.e. 10%)

        Return a set of randomized samples for the chains
        """
        initial_samples = np.empty((walkers, len(parameters)))

        for i in range(walkers):
            accepted = False

            while not accepted:
                # Generate random values centered around the best-fit parameters
                perturbed_params = np.random.normal(parameters, np.abs(parameters) * percent)

                # Check if the perturbed parameters are within the bounds
                #if np.all(np.logical_and(bounds[:, 0] <= perturbed_params, perturbed_params <= bounds[:, 1])):
                #    initial_samples[i] = perturbed_params
                accepted = True
                for j, (lower, upper) in enumerate(bounds):
                    if (lower is not None and perturbed_params[j] < lower) or (upper is not None and perturbed_params[j] > upper):
                        accepted = False
                        break
                if accepted:
                    initial_samples[i] = perturbed_params
        
        return initial_samples


    def get_rstat(self, burnin: int =None):
        """Calculate convergence criterion from Gelman & Rubin 1992; Gelman et al. 2004.
        Values close to 1 indicate convergence.
        Parameters
        ----------
        burnin:int,
            Number of samples to burn prior to the estimation. By default
            uses the autocorrelation time to estimate a suitable number

        Returns an array of the value of the statistic per chain
        """
        if self._sampler is None:
            raise ValueError("Posteriors have not been derived. Please run derive_posteriors prior to populate the attributes.")
        # calculate R stat
        if burnin is None:
            # discard 10 times the mean autocorrelation time
            burnin = int(np.mean(self.tau)) * 10
        samples = self._sampler.get_chain(discard=burnin)

        whithin_chain_variances = np.var(samples, axis=0) # this has nwalkers, ndim (one per chain and param)

        samples = self._sampler.get_chain(flat=True, discard=burnin)
        between_chain_variances = np.var(samples, axis=0)
        return whithin_chain_variances / between_chain_variances[np.newaxis, :]

    @property
    def loglikehoods(self):
        if self._loglikelihoods is None:
            raise AttributeError("Posteriors have not been derived. Please run derive_posteriors prior to populate the attributes.")
        return self._loglikelihoods

    @property
    def autocorr(self):
        return self._autocorr

    @property
    def sampler(self):
        if self._loglikelihoods is None:
            raise AttributeError("Posteriors have not been derived. Please run derive_posteriors prior to populate the attributes.")
        return self._sampler

    @property
    def mcmc_samples(self):
        if self._mcmc_samples is None:
            raise AttributeError("Posteriors have not been derived. Please run derive_posteriors prior to populate the attributes.")
        return self._mcmc_samples

    @property
    def max_loglikehood(self):
        if self._loglikelihoods is None:
            raise AttributeError("Posteriors have not been derived. Please run derive_posteriors prior to populate the attributes.")

        return np.max(self._loglikelihoods)

    @property
    def max_parameters(self):
        """Return the parameters that maximize the loglikehood"""
        if self._mcmc_samples is None:
            raise AttributeError("Posteriors have not been derived. Please run derive_posteriors prior to populate the attributes.")
        return self._mcmc_samples[np.argmax(self._loglikelihoods)]

    @property
    def median_parameters(self):
        """Return the median parameters from the thinned and burned chains"""
        if self._mcmc_samples is None:
            raise AttributeError("Posteriors have not been derived. Please run derive_posteriors prior to populate the attributes.")
        return np.median(self._mcmc_samples, axis=0)

    @property
    def parameter_names(self):
        return gp.get_parameter_names()


    @property
    def k(self):
        """
        Number of variable parameters

        Returns
        -------
        int
            Number of variable parameters
        """
        return self._ndim


    @property
    def tau(self):
        """The autocorrelation time of the chains"""
        if self._mcmc_samples is None:
            raise AttributeError("Posteriors have not been derived. Please run derive_posteriors prior to populate the attributes.")
        return self._tau


    def generate_from_posteriors(self, nsims=10, cpus=8, pdf="Gaussian", extension_factor=2):
        """Generates lightcurves by sampling from the MCMC posteriors

        nsims: int,
            Number of lightcurve simulations to perform
        cpus: int,
            Number of cpus to use for parallelization
        pdf: str,
            PDF for the simulations: Gaussian, Lognormal or Uniform
        extension_factor: int,
            Extension factor for the generation of the lightcurves, to introduce rednoise leakage
        """

        if self._mcmc_samples is None:
            raise RuntimeError("Posteriors have not been derived. Please run derive_posteriors prior to calling this method.")
        if nsims >= len(self._mcmc_samples):
            warnings.warn("The number of simulation requested (%d) is higher than the number of posterior samples (%d), so many samples will be drawn more than once")

        # get some parameter combinations at random
        param_samples = self._mcmc_samples[np.random.randint(len(self._mcmc_samples), size=nsims)]
        warnings.simplefilter('ignore')
        with Pool(processes=cpus, initializer=np.random.seed) as pool:
            lightcurves = pool.map(partial(self._generate_lc_from_params, pdf=pdf, extension_factor=extension_factor), param_samples)
        return lightcurves
    
    def _generate_lc_from_params(self, parameters, pdf, extension_factor):
        self.gp.set_parameter_vector(parameters)
        psd_model = self.gp.kernel.get_psd
        simulator = self._lightcurve.get_simulator(psd_model, pdf)
        rates = simulator.generate_lightcurve(2)
        noisy_rates, dy = simulator.add_noise(rates)
        lc = GappyLightcurve(self._lightcurve.times, noisy_rates, dy)
        return lc