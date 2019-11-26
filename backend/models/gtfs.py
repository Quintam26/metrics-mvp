from datetime import date
import shapely
import partridge as ptg
import numpy as np
from pathlib import Path
import requests
import json
import boto3
import gzip
import hashlib
import zipfile

from . import config, util, nextbus, routeconfig, timetables

def get_stop_geometry(stop_xy, shape_lines_xy, shape_cumulative_dist, start_index):
    # Finds the first position of a particular stop along a shape (after the start_index'th line segment in shape_lines_xy),
    # using XY coordinates in meters.
    # The returned dict is used by the frontend to draw line segments along a route between two stops.

    num_shape_lines = len(shape_lines_xy)

    best_offset = 99999999
    best_index = 0

    shape_index = start_index

    while shape_index < num_shape_lines:
        shape_line_offset = shape_lines_xy[shape_index].distance(stop_xy)

        if shape_line_offset < best_offset:
            best_offset = shape_line_offset
            best_index = shape_index

        if best_offset < 50 and shape_line_offset > best_offset:
            break

        shape_index += 1


    shape_point = shapely.geometry.Point(shape_lines_xy[best_index].coords[0])
    distance_after_shape_point = stop_xy.distance(shape_point)
    distance_to_shape_point = shape_cumulative_dist[best_index]
    stop_dist = distance_to_shape_point + distance_after_shape_point

    if best_offset > 30:
        print(f'   stop_dist = {int(stop_dist)} = ({int(distance_to_shape_point)} + {int(distance_after_shape_point)}),  offset = {int(best_offset)},  after_index = {best_index} ')

    return {
        'distance': int(stop_dist), # total distance in meters along the route shape to this stop
        'after_index': best_index, # the index of the coordinate of the shape just before this stop
        'offset': int(best_offset) # distance in meters between this stop and the closest line segment of shape
    }

def download_gtfs_data(agency: config.Agency, gtfs_cache_dir):
    gtfs_url = agency.gtfs_url
    if gtfs_url is None:
        raise Exception(f'agency {agency.id} does not have gtfs_url in config')

    cache_dir = Path(gtfs_cache_dir)
    if not cache_dir.exists():
        print(f'downloading gtfs data from {gtfs_url}')
        r = requests.get(gtfs_url)

        if r.status_code != 200:
            raise Exception(f"Error fetching {gtfs_url}: HTTP {r.status_code}: {r.text}")

        zip_path = f'{util.get_data_dir()}/gtfs-{agency.id}.zip'

        with open(zip_path, 'wb') as f:
            f.write(r.content)

        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(gtfs_cache_dir)

def is_subsequence(smaller, bigger):
    smaller_len = len(smaller)
    bigger_len = len(bigger)
    if smaller_len > bigger_len:
        return False

    try:
        start_pos = bigger.index(smaller[0])
    except ValueError:
        return False

    end_pos = start_pos+smaller_len
    if end_pos > bigger_len:
        return False

    return smaller == bigger[start_pos:end_pos]

class GtfsScraper:
    def __init__(self, agency: config.Agency):
        self.agency = agency
        self.agency_id = agency_id = agency.id
        gtfs_cache_dir = f'{util.get_data_dir()}/gtfs-{agency_id}'

        download_gtfs_data(agency, gtfs_cache_dir)

        self.feed = feed = ptg.load_geo_feed(gtfs_cache_dir, {})

        print(f"Loading {agency_id} routes...")
        routes_df = feed.routes
        if agency.gtfs_agency_id is not None:
            routes_df = routes_df[routes_df.agency_id == agency.gtfs_agency_id]
        self.gtfs_routes = routes_df

        print(f"Loading {agency_id} trips...")
        trips_df = feed.trips
        trips_df['direction_id'] = trips_df['direction_id'].astype(str)

        #print(f"Loading {agency_id} stop times...")
        #stop_times_df = feed.stop_times

        print(f"Loading {agency_id} stops...")
        stops_df = feed.stops

        #print(f"Loading {agency_id} shapes...")
        #shapes_df = self.feed.shapes

        # gtfs_stop_ids_map allows looking up row from stops.txt via GTFS stop_id
        self.gtfs_stop_ids_map = {stop.stop_id: stop for stop in stops_df.itertuples()}

        # stops_map allows looking up row from stops.txt via OpenTransit stop ID
        stop_id_gtfs_field = agency.stop_id_gtfs_field
        if stop_id_gtfs_field != 'stop_id':
            self.stops_map = {getattr(stop, stop_id_gtfs_field): stop for stop in stops_df.itertuples()}
        else:
            self.stops_map = self.gtfs_stop_ids_map

        self.stop_times_by_trip = None

    def get_services_by_date(self):
        calendar_df = self.feed.calendar
        calendar_dates_df = self.feed.calendar_dates

        dates_map = {}

        for calendar_row in calendar_df.itertuples():

            start_date = calendar_row.start_date # partridge library already parses date strings as Python date objects
            end_date = calendar_row.end_date

            weekdays = []
            if calendar_row.monday == 1:
                weekdays.append(0)
            if calendar_row.tuesday == 1:
                weekdays.append(1)
            if calendar_row.wednesday == 1:
                weekdays.append(2)
            if calendar_row.thursday == 1:
                weekdays.append(3)
            if calendar_row.friday == 1:
                weekdays.append(4)
            if calendar_row.saturday == 1:
                weekdays.append(5)
            if calendar_row.saturday == 1:
                weekdays.append(6)

            service_id = calendar_row.service_id

            for d in util.get_dates_in_range(start_date, end_date, weekdays=weekdays):
                if d not in dates_map:
                    dates_map[d] = []
                dates_map[d].append(service_id)

        for calendar_date_row in calendar_dates_df.itertuples():

            d = calendar_date_row.date

            service_id = calendar_date_row.service_id
            exception_type = calendar_date_row.exception_type
            if exception_type == 1: # 1 = add service to that date
                if d not in dates_map:
                    dates_map[d] = []
                dates_map[d].append(service_id)
            if exception_type == 2: # 2 = remove service from that date
                if d in dates_map:
                    if service_id in dates_map[d]:
                        dates_map[d].remove(service_id)
                    else:
                        print(f"error in GTFS feed: service {service_id} removed from {d}, but it was not scheduled on that date")

        return dates_map

    def save_timetables(self, save_to_s3=False):
        agency_id = self.agency_id

        dates_map = self.get_services_by_date()

        #
        # Typically, many dates have identical scheduled timetables (with times relative to midnight on that date).
        # Instead of storing redundant timetables for each date, store one timetable per route for each unique set of service_ids.
        # Each stored timetable is named with a string 'key' which is unique for each set of service_ids.
        #
        # A "date_keys" JSON object is stored in S3 and the local cache which maps dates to keys.
        #
        # Although the keys could be any string that is legal in paths, for ease of browsing, the keys are chosen to be
        # the string representation of one date with that set of service_ids.

        first_date_for_service_ids_map = {}

        try:
            date_keys = timetables.get_date_keys(agency_id)
        except FileNotFoundError as err:
            date_keys = {}

        for d, service_ids in dates_map.items():
            service_ids = sorted(service_ids)
            service_ids_key = json.dumps(service_ids)
            if service_ids_key not in first_date_for_service_ids_map:
                first_date_for_service_ids_map[service_ids_key] = d

            date_keys[d] = str(first_date_for_service_ids_map[service_ids_key])

        date_keys_cache_path = timetables.get_date_keys_cache_path(agency_id)

        Path(date_keys_cache_path).parent.mkdir(parents = True, exist_ok = True)

        data_str = json.dumps({
            'version': timetables.DefaultVersion,
            'date_keys': {str(d): date_key for d, date_key in date_keys.items()},
        }, separators=(',', ':'))

        with open(date_keys_cache_path, "w") as f:
            f.write(data_str)

        if save_to_s3:
            s3 = boto3.resource('s3')
            s3_path = timetables.get_date_keys_s3_path(agency_id)
            s3_bucket = config.s3_bucket
            print(f'saving to s3://{s3_bucket}/{s3_path}')
            object = s3.Object(s3_bucket, s3_path)
            object.put(
                Body=gzip.compress(bytes(data_str, 'utf-8')),
                CacheControl='max-age=86400',
                ContentType='application/json',
                ContentEncoding='gzip',
                ACL='public-read'
            )

        trips_df = self.feed.trips

        gtfs_route_id_map = {}

        route_configs = routeconfig.get_route_list(self.agency_id) # todo use route config from parsing this GTFS file (will eventually be needed to process old GTFS feeds)
        for route_config in route_configs:
            gtfs_route_id_map[route_config.gtfs_route_id] = route_config

        for gtfs_route_id, route_trips in trips_df.groupby('route_id'):
            route_config = gtfs_route_id_map[gtfs_route_id]

            arrivals_by_service_id = {}
            trip_ids_map = {}

            for service_id, service_route_trips in route_trips.groupby('service_id'):
                arrivals_by_service_id[service_id] = self.get_scheduled_arrivals_by_service_id(service_id, route_config, service_route_trips, trip_ids_map)

            for service_ids_json, d in first_date_for_service_ids_map.items():
                service_ids = json.loads(service_ids_json)

                # merge scheduled arrivals for all service_ids that are in service on the same date
                merged_arrivals = {}

                for service_id in service_ids:
                    if service_id not in arrivals_by_service_id:
                        continue

                    service_id_arrivals = arrivals_by_service_id[service_id]

                    for dir_id, direction_arrivals in service_id_arrivals.items():
                        if dir_id not in merged_arrivals:
                            merged_arrivals[dir_id] = {}

                        direction_merged_arrivals = merged_arrivals[dir_id]

                        for stop_id, stop_arrivals in direction_arrivals.items():

                            if stop_id not in direction_merged_arrivals:
                                direction_merged_arrivals[stop_id] = []

                            direction_merged_arrivals[stop_id] = sorted(direction_merged_arrivals[stop_id] + stop_arrivals, key=lambda arr: arr['t'])

                date_key = str(d)

                cache_path = timetables.get_cache_path(agency_id, route_config.id, date_key)
                Path(cache_path).parent.mkdir(parents = True, exist_ok = True)

                data_str = json.dumps({
                    'version': timetables.DefaultVersion,
                    'agency': agency_id,
                    'route_id': route_config.id,
                    'date_key' : date_key,
                    'timezone_id': self.agency.timezone_id,
                    'service_ids': service_ids,
                    'arrivals': merged_arrivals,
                }, separators=(',', ':'))

                with open(cache_path, "w") as f:
                    f.write(data_str)

                if save_to_s3:
                    s3_path = timetables.get_s3_path(agency_id, route_config.id, date_key)
                    s3 = boto3.resource('s3')
                    s3_bucket = config.s3_bucket
                    print(f'saving to s3://{s3_bucket}/{s3_path}')
                    object = s3.Object(s3_bucket, s3_path)
                    object.put(
                        Body=gzip.compress(bytes(data_str, 'utf-8')),
                        CacheControl='max-age=86400',
                        ContentType='application/json',
                        ContentEncoding='gzip',
                        ACL='public-read'
                    )

    def get_scheduled_arrivals_by_service_id(self, service_id, route_config, service_route_trips, trip_ids_map):

        # returns dict { direction_id => { stop_id => { 't': arrival_time, 'i': trip_int, 'e': departure_time } } }
        # where arrival_time and departure_time are the number of seconds after midnight,
        # and trip_int is a unique integer for each trip (instead of storing GTFS trip ID strings directly)

        agency = self.agency
        agency_id = agency.id

        next_trip_int = 1
        if len(trip_ids_map) > 0:
            next_trip_int = max(trip_ids_map.values()) + 1

        print(f'service={service_id} route={route_config.id} #trips={len(service_route_trips)}')

        arrivals_by_direction = {}

        gtfs_direction_id_map = {}
        for dir_info in route_config.get_direction_infos():
            gtfs_direction_id_map[dir_info.gtfs_direction_id] = dir_info
            arrivals_by_direction[dir_info.id] = {}

        for route_trip in service_route_trips.itertuples():
            trip_id = route_trip.trip_id
            trip_stop_times = self.get_stop_times_for_trip(trip_id)

            if trip_id not in trip_ids_map:
                trip_ids_map[trip_id] = next_trip_int
                next_trip_int += 1

            trip_int = trip_ids_map[trip_id]

            # todo handle custom directions
            dir_info = gtfs_direction_id_map[route_trip.direction_id]

            direction_arrivals = arrivals_by_direction[dir_info.id]

            for stop_time in trip_stop_times.itertuples():
                stop_id = self.normalize_gtfs_stop_id(stop_time.stop_id)
                arrival_time = int(stop_time.arrival_time)
                departure_time = int(stop_time.departure_time)

                arrival_data = {'t': arrival_time, 'i':trip_int}
                if departure_time != arrival_time:
                    arrival_data['e'] = departure_time

                if stop_id not in direction_arrivals:
                    direction_arrivals[stop_id] = []

                direction_arrivals[stop_id].append(arrival_data)

        return arrivals_by_direction

    def get_stop_times_for_trip(self, trip_id):
        if self.stop_times_by_trip is None:
            all_stop_times = self.feed.stop_times
            self.stop_times_by_trip = {trip_id: stop_times for trip_id, stop_times in all_stop_times.groupby('trip_id')}
        return self.stop_times_by_trip[trip_id]

    # get OpenTransit stop ID for GTFS stop_id (may be the same)
    def normalize_gtfs_stop_id(self, gtfs_stop_id):
        stop_id_gtfs_field = self.agency.stop_id_gtfs_field
        if stop_id_gtfs_field != 'stop_id':
            return getattr(self.gtfs_stop_ids_map[gtfs_stop_id], stop_id_gtfs_field)
        else:
            return gtfs_stop_id

    def get_unique_shapes(self, direction_trips_df):
        # Finds the unique shapes associated with a GTFS route/direction, merging shapes that contain common subsequences of stops.
        # These unique shapes may represent multiple branches of a route.
        # Returns a list of dicts with properties 'shape_id', 'count', and 'stop_ids', sorted by count in descending order.

        stop_times_df = self.feed.stop_times

        stop_times_trip_id_values = stop_times_df['trip_id'].values

        direction_shape_id_values = direction_trips_df['shape_id'].values

        unique_shapes_map = {}

        direction_shape_ids, direction_shape_id_counts = np.unique(direction_shape_id_values, return_counts=True)
        direction_shape_id_order = np.argsort(-1 * direction_shape_id_counts)

        direction_shape_ids = direction_shape_ids[direction_shape_id_order]
        direction_shape_id_counts = direction_shape_id_counts[direction_shape_id_order]

        for shape_id, shape_id_count in zip(direction_shape_ids, direction_shape_id_counts):
            shape_trip = direction_trips_df[direction_shape_id_values == shape_id].iloc[0]
            shape_trip_id = shape_trip.trip_id
            shape_trip_stop_times = stop_times_df[stop_times_trip_id_values == shape_trip_id].sort_values('stop_sequence')

            shape_trip_stop_ids = [
                self.normalize_gtfs_stop_id(gtfs_stop_id)
                for gtfs_stop_id in shape_trip_stop_times['stop_id'].values
            ]

            unique_shape_key = hashlib.sha256(json.dumps(shape_trip_stop_ids).encode('utf-8')).hexdigest()[0:12]

            #print(f'  shape {shape_id} ({shape_id_count})')

            if unique_shape_key not in unique_shapes_map:
                for other_shape_key, other_shape_info in unique_shapes_map.items():
                    #print(f"   checking match with {shape_id} and {other_shape_info['shape_id']}")
                    if is_subsequence(shape_trip_stop_ids, other_shape_info['stop_ids']):
                        print(f"    shape {shape_id} is subsequence of shape {other_shape_info['shape_id']}")
                        unique_shape_key = other_shape_key
                        break
                    elif is_subsequence(other_shape_info['stop_ids'], shape_trip_stop_ids):
                        print(f"    shape {other_shape_info['shape_id']} is subsequence of shape {shape_id}")
                        shape_id_count += other_shape_info['count']
                        del unique_shapes_map[other_shape_key]
                        break

            if unique_shape_key not in unique_shapes_map:
                unique_shapes_map[unique_shape_key] = {
                    'count': 0,
                    'shape_id': shape_id,
                    'stop_ids': shape_trip_stop_ids
                }

            unique_shapes_map[unique_shape_key]['count'] += shape_id_count

        sorted_shapes = sorted(unique_shapes_map.values(), key=lambda shape: -1 * shape['count'])

        for shape_info in sorted_shapes:
            count = shape_info['count']
            shape_id = shape_info['shape_id']
            stop_ids = shape_info['stop_ids']

            first_stop_id = stop_ids[0]
            last_stop_id = stop_ids[-1]
            first_stop = self.stops_map[first_stop_id]
            last_stop = self.stops_map[last_stop_id]

            print(f'  shape_id: {shape_id} ({count}x) stops:{len(stop_ids)} from {first_stop_id} {first_stop.stop_name} to {last_stop_id} {last_stop.stop_name} {",".join(stop_ids)}')

        return sorted_shapes

    def get_custom_direction_data(self, custom_direction_info, route_trips_df):
        direction_id = custom_direction_info['id']
        print(f' custom direction = {direction_id}')

        gtfs_direction_id = custom_direction_info['gtfs_direction_id']

        route_direction_id_values = route_trips_df['direction_id'].values

        direction_trips_df = route_trips_df[route_direction_id_values == gtfs_direction_id]

        included_stop_ids = custom_direction_info.get('included_stop_ids', [])
        excluded_stop_ids = custom_direction_info.get('excluded_stop_ids', [])

        shapes = self.get_unique_shapes(direction_trips_df)

        def contains_included_stops(shape_stop_ids):
            min_index = 0
            for stop_id in included_stop_ids:
                try:
                    index = shape_stop_ids.index(stop_id, min_index)
                except ValueError:
                    return False
                min_index = index + 1 # stops must appear in same order as in included_stop_ids
            return True

        def contains_excluded_stop(shape_stop_ids):
            for stop_id in excluded_stop_ids:
                try:
                    index = shape_stop_ids.index(stop_id)
                    return True
                except ValueError:
                    pass
            return False

        matching_shapes = []
        for shape in shapes:
            shape_stop_ids = shape['stop_ids']
            if contains_included_stops(shape_stop_ids) and not contains_excluded_stop(shape_stop_ids):
                matching_shapes.append(shape)

        if len(matching_shapes) != 1:
            matching_shape_ids = [shape['shape_id'] for shape in matching_shapes]
            error_message = f'{len(matching_shapes)} shapes found for route {route_id} with GTFS direction ID {gtfs_direction_id}'
            if len(included_stop_ids) > 0:
                error_message += f" including {','.join(included_stop_ids)}"

            if len(excluded_stop_ids) > 0:
                error_message += f" excluding {','.join(excluded_stop_ids)}"

            if len(matching_shape_ids) > 0:
                error_message += f": {','.join(matching_shape_ids)}"

            raise Exception(error_message)

        matching_shape = matching_shapes[0]
        matching_shape_id = matching_shape['shape_id']
        matching_shape_count = matching_shape['count']

        print(f'  matching shape = {matching_shape_id} ({matching_shape_count} times)')

        return self.get_direction_data(
            id=direction_id,
            gtfs_shape_id=matching_shape_id,
            gtfs_direction_id=gtfs_direction_id,
            stop_ids=matching_shape['stop_ids'],
            title=custom_direction_info.get('title', None)
        )

    def get_default_direction_data(self, direction_id, route_trips_df):
        print(f' default direction = {direction_id}')

        route_direction_id_values = route_trips_df['direction_id'].values

        direction_trips_df = route_trips_df[route_direction_id_values == direction_id]

        shapes = self.get_unique_shapes(direction_trips_df)

        best_shape = shapes[0]
        best_shape_id = best_shape['shape_id']
        best_shape_count = best_shape['count']

        print(f'  most common shape = {best_shape_id} ({best_shape_count} times)')

        return self.get_direction_data(
            id=direction_id,
            gtfs_shape_id=best_shape_id,
            gtfs_direction_id=direction_id,
            stop_ids=best_shape['stop_ids']
        )

    def get_direction_data(self, id, gtfs_shape_id, gtfs_direction_id, stop_ids, title = None):
        agency = self.agency
        if title is None:
            default_direction_info = agency.default_directions.get(gtfs_direction_id, {})
            title_prefix = default_direction_info.get('title_prefix', None)

            last_stop_id = stop_ids[-1]
            last_stop = self.stops_map[last_stop_id]

            if title_prefix is not None:
                title = f"{title_prefix} to {last_stop.stop_name}"
            else:
                title = f"To {last_stop.stop_name}"

        print(f'  title = {title}')

        dir_data = {
            'id': id,
            'title': title,
            'gtfs_shape_id': gtfs_shape_id,
            'gtfs_direction_id': gtfs_direction_id,
            'stops': stop_ids,
            'stop_geometry': {},
        }

        shapes_df = self.feed.shapes

        geometry = shapes_df[shapes_df['shape_id'] == gtfs_shape_id]['geometry'].values[0]

        # partridge returns GTFS geometries for each shape_id as a shapely LineString
        # (https://shapely.readthedocs.io/en/stable/manual.html#linestrings).
        # Each coordinate is an array in [lon,lat] format (note: longitude first, latitude second)
        dir_data['coords'] = [
            {
                'lat': round(coord[1], 5),
                'lon': round(coord[0], 5)
            } for coord in geometry.coords
        ]

        start_lat = geometry.coords[0][1]
        start_lon = geometry.coords[0][0]

        #print(f"  start_lat = {start_lat} start_lon = {start_lon}")

        deg_lat_dist = util.haver_distance(start_lat, start_lon, start_lat-0.1, start_lon)*10
        deg_lon_dist = util.haver_distance(start_lat, start_lon, start_lat, start_lon-0.1)*10

        # projection function from lon/lat coordinates in degrees (z ignored) to x/y coordinates in meters.
        # satisfying the interface of shapely.ops.transform (https://shapely.readthedocs.io/en/stable/manual.html#shapely.ops.transform).
        # This makes it possible to use shapely methods to calculate the distance in meters between geometries
        def project_xy(lon, lat, z=None):
            return (round((lon - start_lon) * deg_lon_dist, 1), round((lat - start_lat) * deg_lat_dist, 1))

        xy_geometry = shapely.ops.transform(project_xy, geometry)

        shape_lon_lat = np.array(geometry).T
        shape_lon = shape_lon_lat[0]
        shape_lat = shape_lon_lat[1]

        shape_prev_lon = np.r_[shape_lon[0], shape_lon[:-1]]
        shape_prev_lat = np.r_[shape_lat[0], shape_lat[:-1]]

        # shape_cumulative_dist[i] is the cumulative distance in meters along the shape geometry from 0th to ith coordinate
        shape_cumulative_dist = np.cumsum(util.haver_distance(shape_lon, shape_lat, shape_prev_lon, shape_prev_lat))

        shape_lines_xy = [shapely.geometry.LineString(xy_geometry.coords[i:i+2]) for i in range(0, len(xy_geometry.coords) - 1)]

        # this is the total distance of the GTFS shape, which may not be exactly the same as the
        # distance along the route between the first and last Nextbus stop
        dir_data['distance'] = int(shape_cumulative_dist[-1])

        print(f"  distance = {dir_data['distance']}")

        # Find each stop along the route shape, so that the frontend can draw line segments between stops along the shape
        start_index = 0

        for stop_id in stop_ids:
            stop = self.stops_map[stop_id]

            # Need to project lon/lat coords to x/y in order for shapely to determine the distance between
            # a point and a line (shapely doesn't support distance for lon/lat coords)
            stop_xy = shapely.geometry.Point(project_xy(stop.geometry.x, stop.geometry.y))

            stop_geometry = get_stop_geometry(stop_xy, shape_lines_xy, shape_cumulative_dist, start_index)

            if stop_geometry['offset'] > 100:
                print(f"    !! bad geometry for stop {stop_id}: {stop_geometry['offset']} m from route line segment")
                continue

            dir_data['stop_geometry'][stop_id] = stop_geometry

            start_index = stop_geometry['after_index']

        return dir_data

    def get_route_data(self, route):
        agency = self.agency
        agency_id = agency.id

        trips_df = self.feed.trips
        stops_df = self.feed.stops
        stop_times = self.feed.stop_times

        gtfs_route_id = route.route_id

        short_name = route.route_short_name
        long_name = route.route_long_name

        if isinstance(short_name, str) and isinstance(long_name, str):
            title = f'{short_name}-{long_name}'
        elif isinstance(short_name, str):
            title = short_name
        else:
            title = long_name

        type = int(route.route_type) if hasattr(route, 'route_type') else None
        url = route.route_url if hasattr(route, 'route_url') and isinstance(route.route_url, str) else None
        color = route.route_color if hasattr(route, 'route_color') and isinstance(route.route_color, str) else None
        text_color = route.route_text_color if hasattr(route, 'route_text_color') and isinstance(route.route_text_color, str) else None

        route_id = getattr(route, agency.route_id_gtfs_field)

        if agency.provider == 'nextbus':
            route_id = route_id.replace('-', '_') # hack to handle muni route IDs where e.g. GTFS has "T-OWL" but nextbus has "T_OWL"
            try:
                nextbus_route_config = nextbus.get_route_config(agency.nextbus_id, route_id)
                title = nextbus_route_config.title
            except Exception as ex:
                print(ex)

        print(f'route {route_id} {title}')

        route_data = {
            'id': route_id,
            'title': title,
            'url': url,
            'type': type,
            'color': color,
            'text_color': text_color,
            'gtfs_route_id': gtfs_route_id,
            'stops': {},
        }

        route_trips_df = trips_df[trips_df['route_id'] == gtfs_route_id]

        if route_id in agency.custom_directions:
            route_data['directions'] = [
                self.get_custom_direction_data(custom_direction_info, route_trips_df)
                for custom_direction_info in agency.custom_directions[route_id]
            ]
        else:
            route_data['directions'] = [
                self.get_default_direction_data(direction_id, route_trips_df)
                for direction_id in np.unique(route_trips_df['direction_id'].values)
            ]

        for dir_data in route_data['directions']:
            for stop_id in dir_data['stops']:
                stop = self.stops_map[stop_id]
                stop_data = {
                    'id': stop_id,
                    'lat': round(stop.geometry.y, 5), # stop_lat in gtfs
                    'lon': round(stop.geometry.x, 5), # stop_lon in gtfs
                    'title': stop.stop_name,
                    'url': stop.stop_url if hasattr(stop, 'stop_url') and isinstance(stop.stop_url, str) else None,
                }
                route_data['stops'][stop_id] = stop_data

        return route_data

    def save_routes(self, save_to_s3=True):
        agency = self.agency
        agency_id = agency.id

        routes_data = []

        routes_df = self.gtfs_routes

        if agency.provider == 'nextbus':
            nextbus_route_order = [route.id for route in nextbus.get_route_list(agency.nextbus_id)]

        for route in routes_df.itertuples():
            route_data = self.get_route_data(route)

            if agency.provider == 'nextbus':
                try:
                    sort_order = nextbus_route_order.index(route_data['id'])
                except ValueError as ex:
                    print(ex)
                    sort_order = None
            else:
                sort_order = int(route.route_sort_order) if hasattr(route, 'route_sort_order') else None

            route_data['sort_order'] = sort_order

            routes_data.append(route_data)

        if routes_data[0]['sort_order'] is not None:
            sort_key = lambda route_data: route_data['sort_order']
        else:
            sort_key = lambda route_data: route_data['id']

        routes_data = sorted(routes_data, key=sort_key)

        data_str = json.dumps({
            'version': routeconfig.DefaultVersion,
            'routes': routes_data
        }, separators=(',', ':'))

        cache_path = routeconfig.get_cache_path(agency_id)

        with open(cache_path, "w") as f:
            f.write(data_str)

        if save_to_s3:
            s3 = boto3.resource('s3')
            s3_path = routeconfig.get_s3_path(agency_id)
            s3_bucket = config.s3_bucket
            print(f'saving to s3://{s3_bucket}/{s3_path}')
            object = s3.Object(s3_bucket, s3_path)
            object.put(
                Body=gzip.compress(bytes(data_str, 'utf-8')),
                CacheControl='max-age=86400',
                ContentType='application/json',
                ContentEncoding='gzip',
                ACL='public-read'
            )
