#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: Niv Drory (drory@astro.as.utexas.edu)
# @Filename: tiledb.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)
#


#
# Interface to database holding a list of tiles to observe.
#


import astropy
import numpy
import warnings
from astropy import units as u
import matplotlib.pyplot as plt
import time
import itertools
import cycler

import lvmsurveysim.target
from lvmsurveysim import IFU, config
from lvmsurveysim.exceptions import LVMSurveyOpsError, LVMSurveyOpsWarning
import lvmsurveysim.utils.spherical
import lvmsurveysim.schedule.opsdb as opsdb
from lvmsurveysim.utils.plot import __MOLLWEIDE_ORIGIN__, get_axes, transform_patch_mollweide, convert_to_mollweide

numpy.seterr(invalid='raise')


__all__ = ['TileDB']


def polygon_perimeter(x, y, n=1.0, min_points=5):
    """ x and y are numpy type arrays. Function returns perimiter values every n-degree in length"""
    x_perimeter = numpy.array([])
    y_perimeter = numpy.array([])
    for x1,x2,y1,y2 in zip(x[:-1], x[1:], y[:-1], y[1:]):
        # Calculate the length of a segment, hopefully in degrees
        dl = ((x2-x1)**2 + (y2-y1)**2)**0.5

        n_dl = numpy.max([int(dl/n), min_points])
        
        if x1 != x2:
            m = (y2-y1)/(x2-x1)
            b = y2 - m*x2

            interp_x = numpy.linspace(x1, x2, num=n_dl, endpoint=False)
            interp_y = interp_x * m + b
        
        else:
            interp_x = numpy.full(n_dl, x1)
            interp_y = numpy.linspace(y1,y2, n_dl, endpoint=False)

        x_perimeter = numpy.append(x_perimeter, interp_x)
        y_perimeter = numpy.append(y_perimeter, interp_y)
    return(x_perimeter, y_perimeter)



class TileDB(object):
    """Database holding a list of tiles to observe. Persistence is provided 
    through the OpsDB interface. The operations SQL database is the default.
    FITS tables are optional for simulations and development work.

    Internally, we hold the database as a `~astropy.table.Table` to make operations 
    on columns (which dominate scheduling) most efficient.

    There are a few special TileIDs that describe virtual Tiles, such as
    the dome flat screen, a test exposure, and a NONE Tile. These allow for a strong
    relationship between the Tile database and the Observation database, such that 
    each Observation points to exactly one Tile. The special Tiles occupy the
    TileIDs below the configuration parameter 'tileid_start', while the survey
    tiles start with 'tileid_start' and extend to higher numbers.

    The tile database also stores some metadata about the Tiles, among others the
    path of the target description file used to generate the tiles, the md5 
    checksum of that file (which is compared against the loaded target list to 
    ensure compatibility) and the tileid_start value. These metadata are stored
    in a separate table in SQL, or as FITS header keywords in the FITS files.

    example usage:
        # tile a survey and save:
        targets = TargetList(target_file='./targets.yaml')
        t = TileDB(targets)
        t.tile_targets()
        OpsDB.save_tiledb(tiledb) # save to SQL
        OpsDB.save_tiledb(tiledb, fits=True, path='tiledb') # save to FITS

        # load a tile database, also loads the targetfile (stored as metadata in the db)
        t = OpsDB.load_tiledb()  # load from SQL
        t = OpsDB.load_tiledb(fits=True, path='tiledb')  # load from FITS

    Attributes
    ----------
    targets : ~lvmsurveysim.target.target.TargetList
        The `~lvmsuveysim.target.target.TargetList` object with the list of
        targets of the survey.
    tile_table : ~astropy.table.Table
        An astropy table with the results of tiling the target list. Includes
        coordinates, priorities, and observing constraints for each unique tile.
        During scheduling, we need fast access to tile data in columnar (numpy.array)
        format for various calculations. A Table is much more conventient for that 
        than keeping the data as a collection of tiles.
        We ensure synchronicity between updates to the Table and updates to the 
        database.
    """

    def __init__(self, targets, tile_tab=None, tileid_start=None):
        """
        Create a TileDB instance.

        Parameters
        ----------
        targets : ~lvmsurveysim.target.target.TargetList
            The `~lvmsuveysim.target.target.TargetList` object with the list of
            targets of the survey.
        tile_tab : ~astropy.table.Table
            Optional, the astropy table containing the tiles. Can be None if tiling has not
            occurred yet
        tileid_start : Integer
            optional, ID of first science tile.
        """
        assert isinstance(targets, lvmsurveysim.target.TargetList), "TargetList object expected in ctor of TileDB"
        self.targets = targets    # instance of lvmsurveysim.target.TargetList
        self.tiles = None         # dict of target-number to list of lvmsurveysim.target.Tile
        self.tile_table = tile_tab# will hold astropy.Table of tile data
        self.tileid_start = tileid_start or int(config['tiledb']['tileid_start']) # start value for tile ids
        assert self.tileid_start > -1, "tileid_start value invalid, must be 0 or greater integer"

    def __repr__(self):
        return (f'<TileDB (N_tiles={len(self.tile_table)})>')


    def tile_targets(self, ifu=None):
        '''
        Tile a set of Targets with a given IFU. Overlapping targets are tiled such
        that the tiles in the higher priority target are retained.

        Parameters
        ----------
        ifu : ~lvmsurveysim.target.ifu.IFU
            The `~lvmsurveysim.target.ifu.IFU` object representing the IFU geometry
            to tile with. If None, it will be read from the config file.
        '''
        self.ifu = ifu or IFU.from_config()

        # dict of target-number to list of lvmsurveysim.target.Tile
        self.tiles = self.targets.get_tiling(ifu=self.ifu, to_frame='icrs')

        self.tiling_type = 'hexagonal'

        # Remove pointings that overlap with other regions.
        self.remove_overlap()

        # create the tile table and calculate/record all the necessary data
        self.create_tile_table()


    def update_status(self, tileid, status):
        """
        Update the status field of a tile.

        Parameters
        ----------
        tileid : Integer
            the tile id of the tile to update.
        status : Integer
            the new status word.
        """
        idx = numpy.where(self.tile_table['TileID'] == tileid)[0]
        if len(idx) != 1:
            raise LVMSurveyOpsError(f'tileid {tileid} not found')

        # Update record in database first, then update the cached table
        s = opsdb.OpsDB.update_tile_status(tileid, status)
        assert s==1, 'Database error, more than one tileid updated.'
        self.tile_table['Status'][idx] = status


    def create_tile_table(self):
        '''
        Collect tile data and convert reformat into an astropy.Table instance.
        '''        
        # Sorted list of target numbers which we will use to create master arrays of data we need
        # for scheduling
        s = sorted(self.tiles)

        # An array with the length of all the pointings indicating the index
        # of the target it correspond to.
        target_idx = numpy.concatenate([numpy.repeat(idx, len(self.tiles[idx])) for idx in s])

        # unique tile IDs
        tileid = numpy.array(range(self.tileid_start, self.tileid_start+len(target_idx)), dtype=int)

        # An array with the target name
        target = numpy.concatenate([numpy.repeat(self.targets[idx].name, len(self.tiles[idx])) for idx in s])

        # An array with the target name
        telescope = numpy.concatenate([numpy.repeat(self.targets[idx].telescope.name, len(self.tiles[idx])) for idx in s])

        # All the coordinates and position angles
        ra = numpy.concatenate([[t.coords.ra.deg for t in self.tiles[idx]] for idx in s])
        dec = numpy.concatenate([[t.coords.dec.deg for t in self.tiles[idx]] for idx in s])
        tile_pa = numpy.concatenate([[t.pa.deg for t in self.tiles[idx]] for idx in s])

        # Create an array of the target's priority for each pointing
        target_prio = numpy.concatenate([numpy.repeat(self.targets[idx].priority, len(self.tiles[idx])) for idx in s])

        # Array with the individual tile priorities
        tile_prio = numpy.concatenate([[t.priority for t in self.tiles[idx]] for idx in s])

        # Array with the total exposure time for each tile
        target_exposure_times = numpy.concatenate(
            [numpy.repeat(self.targets[idx].exptime * self.targets[idx].n_exposures, len(self.tiles[idx]))
             for idx in s])

        # Array with exposure quanta (the minimum time to spend on a tile)
        exposure_quantums = numpy.concatenate(
            [numpy.repeat(self.targets[idx].exptime * self.targets[idx].min_exposures, len(self.tiles[idx]))
             for idx in s])

        # Array with the airmass limit for each pointing
        max_airmass_to_target = numpy.concatenate([numpy.repeat(self.targets[idx].max_airmass, len(self.tiles[idx])) for idx in s])

        # Array with the airmass limit for each pointing
        min_shadowheight_to_target = numpy.concatenate(
            [numpy.repeat(self.targets[idx].min_shadowheight, len(self.tiles[idx]))
             for idx in s])

        # Array with the airmass limit for each pointing
        min_moon_to_target = numpy.concatenate(
            [numpy.repeat(self.targets[idx].min_moon_dist, len(self.tiles[idx]))
             for idx in s])

        # Array with the lunation limit for each pointing
        max_lunation = numpy.concatenate(
            [numpy.repeat(self.targets[idx].max_lunation, len(self.tiles[idx]))
             for idx in s])

        # status flags for the tiles
        status = numpy.full(len(tileid), 0, dtype=numpy.int64)

        # create astropy table with all the data
        self.tile_table = astropy.table.Table(
            [tileid, target_idx, target, telescope, ra, dec, tile_pa, target_prio, tile_prio, 
            max_airmass_to_target, max_lunation, min_shadowheight_to_target, min_moon_to_target, 
            target_exposure_times, exposure_quantums, status],
            names=['TileID', 'TargetIndex', 'Target', 'Telescope', 'RA', 'DEC', 'PA', 'TargetPriority', 'TilePriority', 
                   'AirmassLimit', 'LunationLimit', 'HzLimit', "MoonDistanceLimit",
                   'TotalExptime', 'VisitExptime', 'Status'])


    def remove_overlap(self):
        '''
        Calculate and remove tiles in overlapping target regions. The tile belonging to the 
        higher priority target is retained.

        Modifies the self.tiles list to preserve a set of non-overlapping tiles.
        '''
        # Calculate overlap but don't apply the masks
        overlap = self.get_overlap()

        for ii in self.tiles:
            tname = self.targets[ii].name

            # Remove the overlapping tiles from the pointings and
            # remove their tile priorities.            
            self.tiles[ii] = list(itertools.compress(self.tiles[ii], overlap[tname]['global_no_overlap']))

            if len(self.tiles[ii]) == 0:
                warnings.warn(f'target {tname} completely overlaps with other '
                                'targets with higher priority.', LVMSurveyOpsWarning)

    def get_overlap(self, verbose_level=1):
        """Returns a dictionary of masks with the overlap between regions."""

        overlap = {}

        # Sort priorities.
        s = sorted(self.tiles)

        # Create an array of pointing to priority, one per target
        priorities = numpy.array([self.targets[idx].priority for idx in s])

        # Save the names ... why not
        names = numpy.array([self.targets[idx].name for idx in s])

        sorted_indices = numpy.argsort(priorities)[::-1]

        # Initialise the overlap dictionaries. Set the global_no_overlap to
        # True for all the tiles in the target tiling
        for idx in s:
            overlap[self.targets[idx].name] = {}
            overlap[self.targets[idx].name]['global_no_overlap'] = numpy.ones(len(self.tiles[idx]),
                                                            dtype=numpy.bool)

        # With all the dictionaries created overlap[target_name] we can now store overlap information between targets
        for idx in s:
            if self.targets[idx].overlap == False:
                # s contains the index of all targets. If a target with index idx has overlap False, we need to intialize the dictionaries containing
                # overlap information for later. This includes the overlap of idx with all others, as well as an entry of all other targets and their overlap with idx
                # First create a copy of the target indexs
                tmp_s = s.copy()
                # Now remove idx, so that we can loop over i where i!=j in an efficient way.
                del(tmp_s[idx])
                for j in tmp_s:
                    if j != idx:
                        overlap[self.targets[j].name][self.targets[idx].name] = numpy.full(len(self.tiles[j]), False)
                        overlap[self.targets[idx].name][self.targets[j].name] = numpy.full(len(self.tiles[idx]), False)

        #import spherical geometry routine to use for calculating polygons in spherical coordinates
        from spherical_geometry import polygon as spherical_geometry_polygon

        for index_of_i, target_index_i in enumerate(sorted_indices[:-1]):

            if self.targets[target_index_i].overlap:
                # i has the highest priority because of the [::-1] reversal of the priority list

                if self.targets[target_index_i].region.region_type == 'circle':
                    poly_i = spherical_geometry_polygon.SphericalPolygon.from_cone(self.targets[target_index_i].region.coords.transform_to('icrs').ra.deg,
                    self.targets[target_index_i].region.coords.transform_to('icrs').dec.deg,\
                    self.targets[target_index_i].region.r.deg,
                    degrees=True)

                elif self.targets[target_index_i].region.region_type == 'rectangle':
                    # Create a reference to the target shapley object. This is probably uncessary, and can be sourced directly
                    shapely_i = self.targets[target_index_i].region.shapely

                    # Create a set of polygons using the extertiors reported by shapely to create polygons using a convex hull.
                    # This is probably stupid and I should use the actual polygon methods: rectangle circle, etc.
                    # Get the x-y coordinates which define the polygon of the region.
                    x_i, y_i = shapely_i.exterior.coords.xy

                    per_x, per_y = polygon_perimeter(x_i, y_i)
                    c_poly_perimeter = astropy.coordinates.SkyCoord(per_x*u.degree, per_y*u.degree, frame=self.targets[target_index_i].frame)
                    poly_i = spherical_geometry_polygon.SphericalPolygon.from_radec(c_poly_perimeter.transform_to('icrs').ra.deg, c_poly_perimeter.transform_to('icrs').dec.deg)

                else:
                    # Create a reference to the target shapley object. This is probably uncessary, and can be sourced directly
                    shapely_i = self.targets[target_index_i].region.shapely

                    # Create a set of polygons using the extertiors reported by shapely to create polygons using a convex hull.
                    # This is probably stupid and I should use the actual polygon methods: rectangle circle, etc.
                    # Get the x-y coordinates which define the polygon of the region.
                    x_i, y_i = shapely_i.exterior.coords.xy

                    # Convert the coordinates of the polygon into SkyCoordinates
                    # This logical statemetns that check for the type of coordinate
                    c_i = astropy.coordinates.SkyCoord(x_i*u.degree, y_i*u.degree, frame=self.targets[target_index_i].frame)

                    # Convert the x-y coordinates, now in SkyCoordinates into polygons in icrs. 
                    # This ensures that independent of what ever coordinate system i or j are in that the comparison is in the correct frame
                    poly_i = spherical_geometry_polygon.SphericalPolygon.from_radec(c_i.transform_to('icrs').ra.deg, c_i.transform_to('icrs').dec.deg)


                for j in sorted_indices[index_of_i + 1:]:
                    if self.targets[j].overlap:
                        # j has a lower priority. So we are masking j with i
                        if self.targets[j].region.region_type == 'circle':
                            poly_j = spherical_geometry_polygon.SphericalPolygon.from_cone(self.targets[j].region.coords.transform_to('icrs').ra.deg, self.targets[j].region.coords.transform_to('icrs').dec.deg, self.targets[j].region.r.deg, degrees=True)

                        elif self.targets[j].region.region_type == 'rectangle':
                            # Create a reference to the target shapley object. This is probably uncessary, and can be sourced directly
                            shapely_j = self.targets[j].region.shapely

                            # Create a set of polygons using the extertiors reported by shapely to create polygons using a convex hull.
                            # This is probably stupid and I should use the actual polygon methods: rectangle circle, etc.
                            # Get the x-y coordinates which define the polygon of the region.
                            x_j, y_j = shapely_j.exterior.coords.xy

                            per_x, per_y = polygon_perimeter(x_j, y_j)
                            c_poly_perimeter = astropy.coordinates.SkyCoord(per_x*u.degree, per_y*u.degree, frame=self.targets[target_index_i].frame)
                            poly_j = spherical_geometry_polygon.SphericalPolygon.from_radec(c_poly_perimeter.transform_to('icrs').ra.deg, c_poly_perimeter.transform_to('icrs').dec.deg)

                        else:
                            # Create a reference to the target shapley object. This is probably uncessary, and can be sourced directly 
                            shapely_j = self.targets[j].region.shapely
                            
                            # Create a set of polygons using the extertiors reported by shapely to create polygons using a convex hull.
                            # This is probably stupid and I should use the actual polygon methods: rectangle circle, etc.
                            # Get the x-y coordinates which define the polygon of the region.
                            x_j, y_j = shapely_j.exterior.coords.xy

                            # Convert the coordinates of the polygon into SkyCoordinates
                            # This logical statemetns that check for the type of coordinate
                            c_j = astropy.coordinates.SkyCoord(x_j*u.degree, y_j*u.degree, frame=self.targets[j].frame)

                            # Convert the x-y coordinates, now in SkyCoordinates into polygons in icrs. 
                            # This ensures that independent of what ever coordinate system i or j are in that the comparison is in the correct frame
                            poly_j = spherical_geometry_polygon.SphericalPolygon.from_radec(c_j.transform_to('icrs').ra.deg, c_j.transform_to('icrs').dec.deg)

                        # Use the spherical polygon intersection routine, replacing shapely which only works on cartesian grids.
                        # short circuit the calculation on the tiles if the shapes do not overlap
                        may_overlap = poly_i.intersects_poly(poly_j)

                        # Note before proceeding to the next code block, if you are familiar with previous methods, we no longer have a miss match between
                        # the region polygon coordinate frame and the coordinate frame of the tiles. Everything is converted to ICRS. This prevents us from having to convert everything
                        # to galactic and store values, which took a lot of time for larger targets. This change seems to have off set the cost of looping over all tiles to calculate 
                        # if it is contained by another target.

                        if may_overlap is True:
                            # shapes overlap, so now find all tiles of j that are within i:
                            lon_j = [t.coords.ra.deg for t in self.tiles[j]] 
                            lat_j = [t.coords.dec.deg for t in self.tiles[j]]

                            #Initialize array to True. This doesn't matter. We loop over all values anyway, but it's nice.
                            overlap[names[j]][names[target_index_i]] = numpy.full(len(self.tiles[j]), False)
                            t_start = time.time()
                            # Check array to see which is false.
                            for k in range(len(lon_j)):
                                contains_True_False = poly_i.contains_radec(lon_j[k], lat_j[k], degrees=True)

                                overlap[names[j]][names[target_index_i]][k] = numpy.logical_not(contains_True_False)

                                if contains_True_False and (verbose_level >= 2):
                                    print("%s x %s overlap at %f, %f"%(self.targets[target_index_i].name, self.targets[j].name, lon_j[k], lat_j[k]))
                            
                            if verbose_level >=1:
                                print("%s x %s Overlap loop exec time(s)= %f"%(self.targets[target_index_i].name, self.targets[j].name, time.time()-t_start))
                        else:
                            overlap[names[j]][names[target_index_i]] = numpy.full(len(self.tiles[j]), True)

                        # For functional use, create a global overlap mask, to be used when scheduling
                        overlap[names[j]]['global_no_overlap'] &= overlap[names[j]][names[target_index_i]]

        return overlap


    def plot(self, target=None, projection='mollweide', fast=False, annotate=False):
        """Plots the observed pointings.

        Parameters
        ----------
        target : str
            taget name to plot, None for all targets (default)
        projection : str
            The projection to use, either ``'mollweide'`` or ``'rectangular'``.
        fast : bool
            Plot IFU sized and shaped patches if `False`. This is the default.
            Allows accurate zooming and viewing. If `True`, plot scatter-plot
            dots instead of IFUs, for speed sacrificing accuracy.
            This is MUCH faster.
        annotate : bool
            Write the targets' names next to the target coordinates. Implies
            ``fast=True``.

        Returns
        -------
        figure : `matplotlib.figure.Figure`
            The figure with the plot.

        """

        if annotate is True:
            fast = True

        if target==None:
            data = self.tile_table[self.tile_table['TileID'] >= self.tileid_start] 
        else:
            data = self.tile_table[self.tile_table['Target'] == target]

        color_cycler = cycler.cycler(bgcolor=['b', 'r', 'g', 'y', 'm', 'c', 'k'])

        fig, ax = get_axes(projection=projection)

        if fast is True:
            if projection == 'mollweide':
                x,y = convert_to_mollweide(data['RA'], data['DEC'])
            else:
                x,y = data['RA'], data['DEC']
            tt = [target.name for target in self.targets]
            g = numpy.array([tt.index(i) for i in data['Target']], dtype=float)
            ax.scatter(x, y, c=g % 19, s=0.05, edgecolor=None, edgecolors=None, cmap='tab20')
            if annotate is True:
                _, text_indices = numpy.unique(g, return_index=True)
                for i in range(len(tt)):
                    plt.text(x[text_indices[i]], y[text_indices[i]], tt[i], fontsize=9)
        else:
            ifu = IFU.from_config()
            for ii, sty in zip(range(len(self.targets)), itertools.cycle(color_cycler)):

                target = self.targets[ii]
                name = target.name

                target_data = data[data['Target'] == name]

                patches = [ifu.get_patch(scale=target.telescope.plate_scale, centre=[p['RA'], p['DEC']], pa=p['PA'],
                                         edgecolor='None', linewidth=0.0, facecolor=sty['bgcolor'])[0]
                           for p in target_data]

                if projection == 'mollweide':
                    patches = [transform_patch_mollweide(ax, patch, origin=__MOLLWEIDE_ORIGIN__,
                                                         patch_centre=target_data['RA'][ii])
                               for ii, patch in enumerate(patches)]

                for patch in patches:
                    ax.add_patch(patch)

        return fig


