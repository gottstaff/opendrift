# This file is part of OpenDrift.
#
# OpenDrift is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, version 2
#
# OpenDrift is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with OpenDrift.  If not, see <http://www.gnu.org/licenses/>.
#
# Copyright 2015, Knut-Frode Dagestad, MET Norway

import logging
from datetime import datetime, timedelta
from bisect import bisect_left

import numpy as np
from netCDF4 import Dataset, num2date
import vgrid

from readers import Reader


class Reader(Reader):

    # Map ROMS variable names to CF standard_name
    ROMS_variable_mapping = {
        'mask_psi': 'land_binary_mask',
        'h': 'sea_floor_depth',
        'zeta': 'sea_surface_height',
        'u': 'x_sea_water_velocity',
        'v': 'y_sea_water_velocity',
        'temp': 'sea_water_temperature',
        'salt': 'sea_water_salinity',
        'uice': 'sea_ice_x_velocity',
        'vice': 'sea_ice_y_velocity',
        'aice': 'sea_ice_area_fraction',
        'hice': 'sea_ice_thickness'}

    zbuffer = 1  # Vertical buffer of block around elements

    def __init__(self, filename=None, name=None):

        if filename is None:
            raise ValueError('Need filename as argument to constructor')

        if name is None:
            self.name = filename
        else:
            self.name = name

        try:
            # Open file, check that everything is ok
            logging.info('Opening dataset: ' + filename)
            self.Dataset = Dataset(filename, 'r')
        except:
            raise ValueError('Could not open ' + filename +
                             ' with netCDF4 library')

        # Read sigma-coordinate values
        try:
            self.sigma = self.Dataset.variables['s_rho'][:]
        except:
            num_sigma = len(self.Dataset.dimensions['s_rho'])
            logging.warning('s_rho not available in dataset, constructing from'
                            ' number of layers (%s).' % num_sigma)
            self.sigma = (np.arange(num_sigma)+.5-num_sigma)/num_sigma

        # Read sigma-coordinate transform parameters
        try:
            self.Vtransform = self.Dataset.variables['Vtransform'][:][0]
            self.Vstretching = self.Dataset.variables['Vstretching'][:][0]
            if self.Vtransform == 1 and self.Vstretching == 1:
                self.s_coordinate = vgrid.s_coordinate
            elif self.Vtransform == 2 and self.Vstretching == 2:
                self.s_coordinate = vgrid.s_coordinate_2
            elif self.Vtransform == 2 and self.Vstretching == 4:
                self.s_coordinate = vgrid.s_coordinate_4
            else:
                logging.warning('Sigma-coordinate transformation '
                    'unknown for\n\tVtransform = %s and Vstretching = %s\n'
                    'Defaulting to Vtransform = 1, Vstretching = 1' %
                    (self.Vtransform, self.Vstretching))
                self.s_coordinate = vgrid.s_coordinate
        except Exception as e:
            logging.warning('Sigma-information not available, '
                             'defaulting to\n Vtransform = 1, Vstretching = 1')
            self.s_coordinate = vgrid.s_coordinate

        try:
            self.theta_s = self.Dataset.variables['theta_s'][:][0]
            self.theta_b = self.Dataset.variables['theta_b'][:][0]
            self.Tcline = self.Dataset.variables['Tcline'][:][0]
        except:
            logging.warning('Missing sigma stretching parameters\n'
                            ' - using default values')
            self.theta_b = 0.4
            self.theta_s = 3
            self.Tcline = 10

        self.num_layers = len(self.sigma)

        # Horizontal oordinates and directions
        self.lat = self.Dataset.variables['lat_rho'][:]
        self.lon = self.Dataset.variables['lon_rho'][:]
        self.angle_between_x_and_east = self.Dataset.variables['angle'][:]

        # Get time coverage
        ocean_time = self.Dataset.variables['ocean_time']
        time_units = ocean_time.getncattr('units')
        self.times = num2date(ocean_time[:], time_units)
        self.start_time = self.times[0]
        self.end_time = self.times[-1]
        if len(self.times) > 1:
            self.time_step = self.times[1] - self.times[0]
        else:
            self.time_step = None

        # x and y are rows and columns for unprojected datasets
        self.xmin = 0.
        self.xmax = np.float(len(self.Dataset.dimensions['xi_rho'])) - 1
        self.delta_x = 1.
        self.ymin = 0.
        self.ymax = np.float(len(self.Dataset.dimensions['eta_rho'])) - 1
        self.delta_y = 1.

        self.name = 'roms native'

        # Find all variables having standard_name
        self.variables = []
        for var_name in self.Dataset.variables:
            if var_name in self.ROMS_variable_mapping.keys():
                var = self.Dataset.variables[var_name]
                self.variables.append(self.ROMS_variable_mapping[var_name])

        # Run constructor of parent Reader class
        super(Reader, self).__init__()

    def get_variables(self, requested_variables, time=None,
                      x=None, y=None, z=None, block=False):

        requested_variables, time, x, y, z, outside = self.check_arguments(
            requested_variables, time, x, y, z)

        nearestTime, dummy1, dummy2, indxTime, dummy3, dummy4 = \
            self.nearest_time(time)

        variables = {}

        # Find horizontal indices corresponding to requested x and y
        indx = np.floor((x-self.xmin)/self.delta_x).astype(int)
        indy = np.floor((y-self.ymin)/self.delta_y).astype(int)

        # Find depth levels covering all elements
        if z.min() == 0:
            indz = self.num_layers - 1  # surface layer
            variables['z'] = z

        else:
            # Find the range of sigma0-values covering given z-values
            indz = self.num_layers - 1  # surface layer
            if not hasattr(self, 'sea_floor_depth'):
                logging.debug('Reading sea floor depth...')
                self.sea_floor_depth = self.Dataset.variables['h'][:]

            depth_at_elements = self.sea_floor_depth[indy, indx]
            sigma_transform = self.s_coordinate(
                depth_at_elements, self.theta_b, self.theta_s,
                self.Tcline, len(self.sigma))
            z_profiles = sigma_transform.z_r[:]
            zmins = np.min(z_profiles, axis=1)
            zmaxs = np.max(z_profiles, axis=1)
            # Remember that sigma increases from -1 bottom to 0 surface
            indz_min = np.max(
                (0, bisect_left(zmaxs, z.min())-self.zbuffer-1))
            indz_max = np.min(
                (len(self.sigma)-1,
                bisect_left(zmins, z.max())+self.zbuffer+1))
            indz = np.arange(indz_min, indz_max+1)
            variables['z'] = z_profiles[indz,:]
            #variables['z'] = z_profiles[indz,0]  # TEMPORARY!

        if block is True:
            # Adding buffer, to cover also future positions of elements
            buffer = self.buffer
            indx = np.arange(np.max([0, indx.min()-buffer]),
                             np.min([indx.max()+buffer, self.lon.shape[1]]))
            indy = np.arange(np.max([0, indy.min()-buffer]),
                             np.min([indy.max()+buffer, self.lon.shape[0]]))
        else:
            indx[outside[0]] = 0  # To be masked later
            indy[outside[0]] = 0


        for par in requested_variables:
            varname = [name for name, cf in
                       self.ROMS_variable_mapping.items() if cf == par]
            var = self.Dataset.variables[varname[0]]

            if var.ndim == 2:
                variables[par] = var[indy, indx]
            elif var.ndim == 3:
                variables[par] = var[indxTime, indy, indx]
            elif var.ndim == 4:
                # Temporarily neglecting depth
                variables[par] = var[indxTime, indz, indy, indx]  # NB 0 was 1
            else:
                raise Exception('Wrong dimension of variable: '
                                + self.variable_mapping[par])

            # If 2D array is returned due to the fancy slicing methods
            # of netcdf-python, we need to take the diagonal
            if variables[par].ndim > 1 and block is False:
                variables[par] = variables[par].diagonal()

            # Mask values outside domain
            variables[par] = np.ma.array(variables[par], ndmin=2, mask=False)
            if block is False:
                variables[par].mask[outside[0]] = True

        # Return coordinate system orientation, for vector rotation
        variables['angle_between_x_and_east'] = \
            np.degrees(self.angle_between_x_and_east[np.meshgrid(indy, indx)])

        if 'land_binary_mask' in variables.keys():
            variables['land_binary_mask'] = 1 - variables['land_binary_mask']

        # Store coordinates of returned points
        #try:
        #    variables['z'] = self.z[indz]
        #except:
        #    variables['z'] = 0
        if block is True:
            variables['x'] = indx
            variables['y'] = indy
        else:
            variables['x'] = self.xmin + (indx-1)*self.delta_x
            variables['y'] = self.ymin + (indy-1)*self.delta_y

        variables['time'] = nearestTime

        return variables
