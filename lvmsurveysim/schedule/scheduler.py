#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: Niv Drory (drory@astro.as.utexas.edu)
# @Filename: tiledb.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

import itertools
import os
import warnings

import astropy
import numpy
from astropy import units as u

import lvmsurveysim.target
from lvmsurveysim.schedule.tiledb import TileDB
from lvmsurveysim.schedule.plan import ObservingPlan
from lvmsurveysim import IFU, config, log
from lvmsurveysim.exceptions import LVMSurveySimError, LVMSurveySimWarning
from lvmsurveysim.schedule.altitude_calc import AltitudeCalculator

import skyfield.api
from lvmsurveysim.utils import shadow_height_lib


numpy.seterr(invalid='raise')


__all__ = ['Scheduler']



class Scheduler(object):
    """Selects optimal tile from a list of targets (tile database) at a given JD

    Parameters
    ----------
    observing_plan : `.ObservingPlan`
        The `.ObservingPlan` to use. Contains dates and sun/moon data for the 
        duration of the survey as well as Observatory data.
    """

    def __init__(self, observing_plan, verbos_level=0):

        assert isinstance(observing_plan, ObservingPlan), 'observing_plan is not an instance of ObservingPlan.'
        self.observing_plan = observing_plan

        self.verbos_level = verbos_level
        self.zenith_avoidance = config['scheduler']['zenith_avoidance']

        eph = skyfield.api.load('de421.bsp')
        self.shadow_calc = shadow_height_lib.shadow_calc(observatory_name=observing_plan.observatory, 
                                observatory_elevation=observing_plan.location.height,
                                observatory_lat=observing_plan.location.lat.deg, 
                                observatory_lon=observing_plan.location.lon.deg,
                                eph=eph, earth=eph['earth'], sun=eph['sun'])


    def __repr__(self):
        return (f'<Scheduler (observing_plans={self.observing_plan.observatory})> ')


    def prepare_for_night(self, jd, plan, tiledb):
        """Schedules a single night at a single observatory.

        This method is not intended to be called directly. Instead, use `.run`.

        Parameters
        ----------
        jd : int
            The Julian Date of the night to schedule. Must be included in ``plan``.
        plan : .ObservingPlan
            The observing plan containing at least the night.
        tiledb : .TileDB
            The tile database for the night (or survey)
        """

        assert isinstance(tiledb, TileDB), 'tiledb must be a lvmsurveysim.schedule.tiledb.TileDB instances.'
        self.tiledb = tiledb

        assert isinstance(plan, ObservingPlan), \
            'one of the items in observing_plans is not an instance of ObservingPlan.'
        self.observatory = plan.observatory
        self.lon = plan.location.lon.deg
        self.lat = plan.location.lat.deg

        self.maxpriority = max([t.priority for t in tiledb.targets])

        night_plan = plan[plan['JD'] == jd]
        self.evening_twi = night_plan['evening_twilight'][0]
        self.morning_twi = night_plan['morning_twilight'][0]

        # Get the Moon lunation and distance to targets, assume it is constant
        # for the night for speed.
        self.lunation = night_plan['moon_phase'][0]

        ra = self.tiledb.tile_table['RA'].data
        dec = self.tiledb.tile_table['DEC'].data
        
        self.moon_to_pointings = lvmsurveysim.utils.spherical.great_circle_distance(
                                 night_plan['moon_ra'], night_plan['moon_dec'], ra, dec)

        # set the coordinates to all targets in shadow height calculator
        self.shadow_calc.set_coordinates(ra, dec)

        # Fast altitude calculator
        self.ac = AltitudeCalculator(ra, dec, self.lon, self.lat)

        # convert airmass to altitude, we'll work in altitude space for efficiency
        tdb = self.tiledb.tile_table
        self.min_alt_for_target = 90.0 - numpy.rad2deg(numpy.arccos(1.0 / tdb['AirmassLimit'].data))

        # Select targets that are above the max airmass and with good
        # moon avoidance.
        self.moon_ok = (self.moon_to_pointings > tdb['MoonDistanceLimit'].data) & (self.lunation <= tdb['LunationLimit'].data)


    def get_optimal_tile(self, jd, observed):
        """Returns the next tile to observe at a given jd

        Parameters
        ----------
        jd : int
            The Julian Date to schedule. Must be included in ``plan``.

        observed : ~numpy.array
            Same length as len(tiledb).
            Array containing the exposure time already executed for each tile in the tiledb.
            This is used to keep track of which tiles need additional time and which are completed.

            This record is kept by the caller and passed in, since an observation might fail 
            in real life.

        Returns
        -------
        observed_idx : int
            Index into the tiledb of the tile to be observed next.
        current_lst : float
            The LST of the observation
        hz : float
            The shadow height for the observation
        alt : float
            The altitude of the observation
        lunation : float
            The lunation at time of the observation
        """

        assert jd < self.morning_twi, "Twilight reached."
        assert jd >= self.evening_twi, "Night not started yet."
        tdb = self.tiledb.tile_table
        assert len(tdb) == len(observed), "observed array and tiledb do not match."

        # Get current LST
        lst = lvmsurveysim.utils.spherical.get_lst(jd, self.lon)

        # advance shadow height calculator to current time
        self.shadow_calc.update_time(jd=jd)

        # Get the altitude at the start and end of the proposed exposure.
        alt_start = self.ac(lst=lst)
        alt_end = self.ac(lst=(lst + (tdb['VisitExptime'].data / 3600.)))

        # avoid the zenith!
        alt_ok = (alt_start < (90 - self.zenith_avoidance)) & (alt_end < (90 - self.zenith_avoidance))

        # Gets valid airmasses (but we're working in altitude space)
        airmass_ok = ((alt_start > self.min_alt_for_target) & (alt_end > self.min_alt_for_target))

        # Gets pointings that haven't been completely observed
        exptime_ok = observed < tdb['TotalExptime'].data

        # Creates a mask of viable pointings with correct Moon avoidance,
        # airmass, zenith avoidance and that have not been completed.
        valid_mask = alt_ok & self.moon_ok & airmass_ok & exptime_ok

        # calculate shadow heights, but only for the viable pointings since it is a costly computation
        hz = numpy.full(len(alt_ok), 0.0)
        hz_valid = self.shadow_calc.get_heights(return_heights=True, mask=valid_mask, unit="km")
        hz[valid_mask] = hz_valid
        hz_ok = (hz > tdb['HzLimit'].data)

        # add shadow height to the viability criteria of the pointings to create the final 
        # subset that are candidates for observation
        valid_idx = numpy.where(valid_mask & hz_ok)[0]

        # If there's nothing to observe, return -1
        if len(valid_idx) == 0:
            return -1, lst, 0, 0, self.lunation

        # Find observations that have nonzero exposure but are incomplete
        incomplete = (observed > 0) & (observed < tdb['TotalExptime'].data)

        target_priorities = tdb['TargetPriority'].data
        tile_priorities = tdb['TilePriority'].data

        # Gets the coordinates, altitudes, and priorities of possible pointings.
        valid_alt = alt_start[valid_idx]
        valid_priorities = target_priorities[valid_idx]
        valid_incomplete = incomplete[valid_idx]
        valid_tile_priorities = tile_priorities[valid_idx]

        # Give incomplete observations the highest priority, imitating a high-priority target,
        # that makes sure these are completed first in all visible targets
        valid_priorities[valid_incomplete] = self.maxpriority + 1

        # Loops starting with targets with the highest priority (lowest numerical value).
        for priority in numpy.flip(numpy.unique(valid_priorities), axis=0):

            # Gets the indices that correspond to this priority (note that
            # these indices correspond to positions in valid_idx, not in the
            # master list).
            valid_priority_idx = numpy.where(valid_priorities == priority)[0]

            # If there's nothing to do at the current priority, try the next lower
            if len(valid_priority_idx) == 0:
                continue

            # select all pointings with the current target priority
            valid_alt_target_priority = valid_alt[valid_priority_idx]
            valid_alt_tile_priority = valid_tile_priorities[valid_priority_idx]

            # Find the tiles with the highest tile priority
            max_tile_priority = numpy.max(valid_alt_tile_priority)
            high_priority_tiles = numpy.where(valid_alt_tile_priority == max_tile_priority)[0]

            # Gets the pointing with the highest altitude * shadow height
            obs_alt_idx = (valid_alt_target_priority[high_priority_tiles]).argmax()
            #obs_alt_idx = (hz[valid_priority_idx[high_priority_tiles]] * valid_alt_target_priority[high_priority_tiles]).argmax()
            #obs_alt_idx = hz[valid_priority_idx[high_priority_tiles]].argmax()
            obs_tile_idx = high_priority_tiles[obs_alt_idx]
            obs_alt = valid_alt_target_priority[obs_tile_idx]

            # Gets the index of the pointing in the master list.
            observed_idx = valid_idx[valid_priority_idx[obs_tile_idx]]

            return observed_idx, lst, hz[observed_idx], obs_alt, self.lunation

        assert False, "Unreachable code!"
        return -1,0   # should never be reached


