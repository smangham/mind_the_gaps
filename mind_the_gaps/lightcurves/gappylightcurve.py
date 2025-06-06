# @Author: Andrés Gúrpide <agurpide>
# @Date:   05-02-2022
# @Email:  agurpidelash@irap.omp.eu
# @Last modified by:   agurpide
# @Last modified time: 28-02-2022
import numpy as np
from typing import List, Union
from numpy import floating
from numpy.typing import ArrayLike, NDArray
from random import sample
from mind_the_gaps.simulator import Simulator
from astropy.modeling import Model as AstropyModel


class ExposureTimeError(Exception):
    def __init__(self, message):
        super().__init__(message)


class GappyLightcurve:
    """
    A class to store parameters of a irregularly-sampled lightcurve
    """
    def __init__(
            self,
            times: ArrayLike,
            y: ArrayLike,
            dy: Union[ArrayLike, None] = None,
            exposures: Union[float, ArrayLike, None] = None,
            bkg_rate: Union[ArrayLike, None] = None,
            bkg_rate_err: Union[ArrayLike, None] = None
    ):
        """

        Parameters
        ----------
        times:
            Timestamps of the lightcurve (i.e. times at the "center" of the sampling). Always in seconds
        y:
            Observed flux or count rate
        dy:
            1 sigma uncertainty on y. Optional, 
        exposures:
            Exposure time of each datapoint in seconds
        bkg_rate:
            Associated background rate (or flux) with each datapoint
        bkg_rate_err:
            uncertainty on background rate

        """
        self._times = times
        self._y = y
        self._dy = dy

        # exposures were given
        if exposures is not None:
            if np.isscalar(exposures):
                self._exposures = np.full(len(times), exposures)
            else:
                self._exposures = exposures
            epsilon = 1.01 # to avoid numerically distinct but equal
            wrong = np.count_nonzero(np.diff(self._times) < self._exposures[:-1] * epsilon / 2 )
            if wrong >0:
                raise ExposureTimeError("Some timestamps (%d) have a spacing below the exposure sampling time!" % wrong)
        
        else:
            self._exposures = np.zeros(len(times))

        self._bkg_rate = bkg_rate if bkg_rate is not None else np.zeros(len(times))
        self._bkg_rate_err = bkg_rate_err if bkg_rate_err is not None else np.zeros(len(times))

    @property
    def times(self) -> ArrayLike:
        """
        Timestamps of the lightcurve.

        Returns
        -------
        ArrayLike
            Timestamps of the lightcurve.
        """
        return self._times

    @property
    def n(self) -> int:
        """
        Number of datapoints

        Returns
        -------
        int
            Number of datapoints
        """
        return len(self._times)

    @property
    def y(self) -> ArrayLike:
        """
        Observed flux or count rate.

        Returns
        -------
        ArrayLike
            Observed flux or count rate.
        """
        return self._y

    @property
    def dy(self) -> ArrayLike:
        """
        1 sigma uncertainty on y.

        Returns
        -------
        ArrayLike
            1 sigma uncertainty on y.
        """
        return self._dy

    @property
    def exposures(self) -> ArrayLike:
        """
        Exposure time of each datapoint.

        Returns
        -------
        ArrayLike
            Exposure time of each datapoint.
        """
        return self._exposures

    @property
    def bkg_rate(self):
        """
        Associated background rate (or flux) with each datapoint.

        Returns
        -------
        ArrayLike
            Associated background rate (or flux) with each datapoint.
        """
        return self._bkg_rate

    @property
    def bkg_rate_err(self):
        """
        Uncertainty on background rate.

        Returns
        -------
        ArrayLike
            Uncertainty on background rate.
        """
        return self._bkg_rate_err

    @property
    def duration(self):
        """
        Duration of the lightcurve (times[-1] - times[0]).

        Returns
        -------
        float
            Duration of the lightcurve.
        """
        return self._times[-1] - self._times[0]

    @property
    def mean(self) -> floating:
        """Mean count rate"""
        return np.mean(self._y)


    def truncate(
            self,
            tmin: floating = -np.inf,
            tmax: floating = np.inf
    ):
        """
        Create a new GappyLightcurve instance by cutting the data between tmin and tmax.

        Parameters
        ----------
        tmin
            Minimum timestamp for the cut.
        tmax
            Maximum timestamp for the cut.

        Returns
        -------
        GappyLightcurve
            New instance representing the cut data.
        """
        if tmin >= tmax:
            raise ValueError("Minimum truncation time (%.2es) is greater than or equal to maximum truncation time (%.3es)!" %(tmin, tmax))
        if tmax < self._times[0]:
            raise ValueError("Maximum truncation time (%.2f) is lower than initial lightcurve time (%.2f)" % (tmax, self._times[0]))
        mask = (self._times >= tmin) & (self._times <= tmax)

        return GappyLightcurve(
            self._times[mask],
            self._y[mask],
            self._dy[mask],
            self._exposures[mask],
            self._bkg_rate[mask],
            self._bkg_rate_err[mask]
        )

    def split(
            self,
            interval: float
    ) -> List['GappyLightcurve']:
        """
        Split the lightcurve based on the input (time) interval

        Parameters
        ----------
        interval
            Time interval with which split the lightcurve

        Returns
        -------
        List[GappyLightcurve]
            Each split lightcurve will be stored as a new object
        """
        lightcurves = []
        # find places where the spacing is larger than the interval
        indexes = np.where(np.diff(self.times) > interval)[0]
        # add last index
        indexes = np.append(indexes, -1)
        j = 0
        for i in indexes:
            lightcurves.append(self.truncate(self.times[j], self.times[i]))
            j = i + 1
        return lightcurves

    def rand_remove(
            self,
            points_remove: int
    ) -> 'GappyLightcurve':
        """Randomly remove a given number of points from the lightcurve"""
        if points_remove > self.n:
            return ValueError("Number of points to remove (%d) is greater than number of lightcurve datapoints (%d)"  % (points_remove, self.n))
        ints = sample(range(len(self._times)), points_remove)
        mask = np.ones(len(self._times), dtype=bool)
        mask[ints] = False
        return GappyLightcurve(
            self._times[mask],
            self._y[mask],
            self._dy[mask],
            self._exposures[mask],
            self._bkg_rate[mask],
            self._bkg_rate_err[mask]
        )

    def to_csv(
            self,
            outname: str
    ):
        """Save lightcurve properties to csv file"""
        outputs = np.array([self._times, self._y, self._dy, self._exposures, self._bkg_rate, self._bkg_rate_err])
        np.savetxt(outname, outputs.T, fmt="%.8e\t%.5f\t%.5f\t%.3f\t%.5f\t%.5f", header="t\trate\terror\texposure\tbkg_rate\tbkg_rate_err")


    def get_simulator(
            self,
            psd_model: AstropyModel,
            pdf: str = "gaussian",
            **kwargs
    ) -> Simulator:
        """
        Creates an instance of mind_the_gaps.Simulator based on the lightcurve
        properties (timestamps, exposures, etc)

        Parameters
        ----------
        psd_model
            The model for the PSD
        pdf
            A string defining the probability distribution (Gaussian, Lognormal or Uniform)
        kwargs
            Arguments for the simulator (aliasing_factor, sigma_noise, etc)

        Returns
        -------
        Simulator
            An instance of the Simulator class initialized with the lightcurve properties.
        """
        return Simulator(psd_model, self._times, self._exposures, self.mean, pdf,
                         self._bkg_rate, self._bkg_rate_err, **kwargs)
