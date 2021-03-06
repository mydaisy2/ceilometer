# -*- encoding: utf-8 -*-
# Copyright © 2012 New Dream Network, LLC (DreamHost)
# Copyright © 2013 eNovance
# Copyright © 2013 IBM Corp
#
# Author: Doug Hellmann <doug.hellmann@dreamhost.com>
#         Julien Danjou <julien@danjou.info>
#         Tong Li <litong01@us.ibm.com>
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
"""DB2 storage backend
"""

import copy
import weakref

import bson.code
import bson.objectid
import pymongo

from ceilometer.openstack.common.gettextutils import _
from ceilometer.openstack.common import log
from ceilometer import storage
from ceilometer.storage import base
from ceilometer.storage import models

LOG = log.getLogger(__name__)


class DB2Storage(base.StorageEngine):
    """The db2 storage for Ceilometer

    Collections::

        - user
          - { _id: user id
              source: [ array of source ids reporting for the user ]
              }
        - project
          - { _id: project id
              source: [ array of source ids reporting for the project ]
              }
        - meter
          - the raw incoming data
        - resource
          - the metadata for resources
          - { _id: uuid of resource,
              metadata: metadata dictionaries
              user_id: uuid
              project_id: uuid
              meter: [ array of {counter_name: string, counter_type: string,
                                 counter_unit: string} ]
            }
    """

    def get_connection(self, conf):
        """Return a Connection instance based on the configuration settings.
        """
        return Connection(conf)


def make_timestamp_range(start, end,
                         start_timestamp_op=None, end_timestamp_op=None):
    """Given two possible datetimes and their operations, create the query
    document to find timestamps within that range.
    By default, using $gte for the lower bound and $lt for the
    upper bound.
    """
    ts_range = {}

    if start:
        if start_timestamp_op == 'gt':
            start_timestamp_op = '$gt'
        else:
            start_timestamp_op = '$gte'
        ts_range[start_timestamp_op] = start

    if end:
        if end_timestamp_op == 'le':
            end_timestamp_op = '$lte'
        else:
            end_timestamp_op = '$lt'
        ts_range[end_timestamp_op] = end
    return ts_range


def make_query_from_filter(sample_filter, require_meter=True):
    """Return a query dictionary based on the settings in the filter.

    :param filter: SampleFilter instance
    :param require_meter: If true and the filter does not have a meter,
                          raise an error.
    """
    q = {}

    if sample_filter.user:
        q['user_id'] = sample_filter.user
    if sample_filter.project:
        q['project_id'] = sample_filter.project

    if sample_filter.meter:
        q['counter_name'] = sample_filter.meter
    elif require_meter:
        raise RuntimeError('Missing required meter specifier')

    ts_range = make_timestamp_range(sample_filter.start, sample_filter.end,
                                    sample_filter.start_timestamp_op,
                                    sample_filter.end_timestamp_op)
    if ts_range:
        q['timestamp'] = ts_range

    if sample_filter.resource:
        q['resource_id'] = sample_filter.resource
    if sample_filter.source:
        q['source'] = sample_filter.source

    # so the samples call metadata resource_metadata, so we convert
    # to that.
    q.update(dict(('resource_%s' % k, v)
                  for (k, v) in sample_filter.metaquery.iteritems()))
    return q


class ConnectionPool(object):

    def __init__(self):
        self._pool = {}

    def connect(self, url):
        if url in self._pool:
            client = self._pool.get(url)()
            if client:
                return client
        LOG.info('connecting to DB2 on %s', url)
        client = pymongo.MongoClient(
            url,
            safe=True)
        self._pool[url] = weakref.ref(client)
        return client


class Connection(base.Connection):
    """DB2 connection.
    """

    CONNECTION_POOL = ConnectionPool()

    GROUP = {'_id': '$counter_name',
             'unit': {'$min': '$counter_unit'},
             'min': {'$min': '$counter_volume'},
             'max': {'$max': '$counter_volume'},
             'sum': {'$sum': '$counter_volume'},
             'count': {'$sum': 1},
             'duration_start': {'$min': '$timestamp'},
             'duration_end': {'$max': '$timestamp'},
             }

    PROJECT = {'_id': 0, 'unit': 1,
               'min': 1, 'max': 1, 'sum': 1, 'count': 1,
               'avg': {'$divide': ['$sum', '$count']},
               'duration_start': 1,
               'duration_end': 1,
               }

    def __init__(self, conf):
        url = conf.database.connection

        # Since we are using pymongo, even though we are connecting to DB2
        # we still have to make sure that the scheme which used to distinguish
        # db2 driver from mongodb driver be replaced so that pymongo will not
        # produce an exception on the scheme.
        url = url.replace('db2:', 'mongodb:', 1)
        self.conn = self.CONNECTION_POOL.connect(url)

        # Require MongoDB 2.2 to use aggregate(), since we are using mongodb
        # as backend for test, the following code is necessary to make sure
        # that the test wont try aggregate on older mongodb during the test.
        # For db2, the versionArray won't be part of the server_info, so there
        # will not be exception when real db2 gets used as backend.
        version_array = self.conn.server_info().get('versionArray')
        if version_array and version_array < [2, 2]:
            raise storage.StorageBadVersion("Need at least MongoDB 2.2")

        connection_options = pymongo.uri_parser.parse_uri(url)
        self.db = getattr(self.conn, connection_options['database'])

        self.upgrade()

    def upgrade(self, version=None):
        # Establish indexes
        #
        # We need variations for user_id vs. project_id because of the
        # way the indexes are stored in b-trees. The user_id and
        # project_id values are usually mutually exclusive in the
        # queries, so the database won't take advantage of an index
        # including both.
        if self.db.resource.index_information() == {}:
            resource_id = str(bson.objectid.ObjectId())
            self.db.resource.insert({'_id': resource_id,
                                     'no_key': resource_id})
            meter_id = str(bson.objectid.ObjectId())
            self.db.meter.insert({'_id': meter_id,
                                  'no_key': meter_id})

            self.db.resource.ensure_index([
                ('user_id', pymongo.ASCENDING),
                ('project_id', pymongo.ASCENDING),
                ('source', pymongo.ASCENDING)], name='resource_idx')

            self.db.meter.ensure_index([
                ('resource_id', pymongo.ASCENDING),
                ('user_id', pymongo.ASCENDING),
                ('project_id', pymongo.ASCENDING),
                ('counter_name', pymongo.ASCENDING),
                ('timestamp', pymongo.ASCENDING),
                ('source', pymongo.ASCENDING)], name='meter_idx')

            self.db.meter.ensure_index([('timestamp',
                                         pymongo.DESCENDING)],
                                       name='timestamp_idx')

            self.db.resource.remove({'_id': resource_id})
            self.db.meter.remove({'_id': meter_id})

            # The following code is to ensure that the keys for collections
            # are set as objectId so that db2 index on key can be created
            # correctly
            user_id = str(bson.objectid.ObjectId())
            self.db.user.insert({'_id': user_id})
            self.db.user.remove({'_id': user_id})

            project_id = str(bson.objectid.ObjectId())
            self.db.project.insert({'_id': project_id})
            self.db.project.remove({'_id': project_id})

    def clear(self):
        # db2 does not support drop_database, remove all collections
        for col in ['user', 'project', 'resource', 'meter']:
            self.db[col].drop()
        # drop_database command does nothing on db2 database since this has
        # not been implemented. However calling this method is important for
        # removal of all the empty dbs created during the test runs since
        # test run is against mongodb on Jenkins
        self.conn.drop_database(self.db)

    def record_metering_data(self, data):
        """Write the data to the backend storage system.

        :param data: a dictionary such as returned by
                     ceilometer.meter.meter_message_from_counter
        """
        # Make sure we know about the user and project
        self.db.user.update(
            {'_id': data['user_id']},
            {'$addToSet': {'source': data['source'],
                           },
             },
            upsert=True,
        )
        self.db.project.update(
            {'_id': data['project_id']},
            {'$addToSet': {'source': data['source'],
                           },
             },
            upsert=True,
        )

        # Record the updated resource metadata
        self.db.resource.update(
            {'_id': data['resource_id']},
            {'$set': {'project_id': data['project_id'],
                      'user_id': data['user_id'],
                      'metadata': data['resource_metadata'],
                      'source': data['source'],
                      },
             '$addToSet': {'meter': {'counter_name': data['counter_name'],
                                     'counter_type': data['counter_type'],
                                     'counter_unit': data['counter_unit'],
                                     },
                           },
             },
            upsert=True,
        )

        # Record the raw data for the meter. Use a copy so we do not
        # modify a data structure owned by our caller (the driver adds
        # a new key '_id').
        record = copy.copy(data)
        # Make sure that the data does have field _id which db2 wont add
        # automatically.
        if record.get('_id') is None:
            record['_id'] = str(bson.objectid.ObjectId())
        self.db.meter.insert(record)

    def clear_expired_metering_data(self, ttl):
        """Clear expired data from the backend storage system according to the
        time-to-live.

        :param ttl: Number of seconds to keep records for.

        """
        raise NotImplementedError('TTL not implemented.')

    def get_users(self, source=None):
        """Return an iterable of user id strings.

        :param source: Optional source filter.
        """
        q = {}
        if source is not None:
            q['source'] = source

        return (doc['_id'] for doc in
                self.db.user.find(q, fields=['_id'],
                                  sort=[('_id', pymongo.ASCENDING)]))

    def get_projects(self, source=None):
        """Return an iterable of project id strings.

        :param source: Optional source filter.
        """
        q = {}
        if source is not None:
            q['source'] = source

        return (doc['_id'] for doc in
                self.db.project.find(q, fields=['_id'],
                                     sort=[('_id', pymongo.ASCENDING)]))

    def get_resources(self, user=None, project=None, source=None,
                      start_timestamp=None, start_timestamp_op=None,
                      end_timestamp=None, end_timestamp_op=None,
                      metaquery={}, resource=None, pagination=None):
        """Return an iterable of models.Resource instances

        :param user: Optional ID for user that owns the resource.
        :param project: Optional ID for project that owns the resource.
        :param source: Optional source filter.
        :param start_timestamp: Optional modified timestamp start range.
        :param start_timestamp_op: Optional start time operator, like gt, ge.
        :param end_timestamp: Optional modified timestamp end range.
        :param end_timestamp_op: Optional end time operator, like lt, le.
        :param metaquery: Optional dict with metadata to match on.
        :param resource: Optional resource filter.
        :param pagination: Optional pagination query.
        """

        if pagination:
            raise NotImplementedError(_('Pagination not implemented'))

        q = {}
        if user is not None:
            q['user_id'] = user
        if project is not None:
            q['project_id'] = project
        if source is not None:
            q['source'] = source
        if resource is not None:
            q['resource_id'] = resource
        # Add resource_ prefix so it matches the field in the db
        q.update(dict(('resource_' + k, v)
                      for (k, v) in metaquery.iteritems()))

        if start_timestamp or end_timestamp:
            # Look for resources matching the above criteria and with
            # samples in the time range we care about, then change the
            # resource query to return just those resources by id.
            ts_range = make_timestamp_range(start_timestamp, end_timestamp,
                                            start_timestamp_op,
                                            end_timestamp_op)
            if ts_range:
                q['timestamp'] = ts_range

        # FIXME(jd): We should use self.db.meter.group() and not use the
        # resource collection, but that's not supported by MIM, so it's not
        # easily testable yet. Since it was bugged before anyway, it's still
        # better for now.
        resource_ids = self.db.meter.find(q).distinct('resource_id')
        q = {'_id': {'$in': resource_ids}}
        for resource in self.db.resource.find(q):
            yield models.Resource(
                resource_id=resource['_id'],
                project_id=resource['project_id'],
                first_sample_timestamp=None,
                last_sample_timestamp=None,
                source=resource['source'],
                user_id=resource['user_id'],
                metadata=resource['metadata'],
                meter=[
                    models.ResourceMeter(
                        counter_name=meter['counter_name'],
                        counter_type=meter['counter_type'],
                        counter_unit=meter.get('counter_unit', ''),
                    )
                    for meter in resource['meter']
                ],
            )

    def get_meters(self, user=None, project=None, resource=None, source=None,
                   metaquery={}, pagination=None):
        """Return an iterable of models.Meter instances

        :param user: Optional ID for user that owns the resource.
        :param project: Optional ID for project that owns the resource.
        :param resource: Optional resource filter.
        :param source: Optional source filter.
        :param metaquery: Optional dict with metadata to match on.
        :param pagination: Optional pagination query.
        """

        if pagination:
            raise NotImplementedError(_('Pagination not implemented'))

        q = {}
        if user is not None:
            q['user_id'] = user
        if project is not None:
            q['project_id'] = project
        if resource is not None:
            q['_id'] = resource
        if source is not None:
            q['source'] = source
        q.update(metaquery)

        for r in self.db.resource.find(q):
            for r_meter in r['meter']:
                yield models.Meter(
                    name=r_meter['counter_name'],
                    type=r_meter['counter_type'],
                    # Return empty string if 'counter_unit' is not valid for
                    # backward compatibility.
                    unit=r_meter.get('counter_unit', ''),
                    resource_id=r['_id'],
                    project_id=r['project_id'],
                    source=r['source'],
                    user_id=r['user_id'],
                )

    def get_samples(self, sample_filter, limit=None):
        """Return an iterable of model.Sample instances.

        :param sample_filter: Filter.
        :param limit: Maximum number of results to return.
        """
        if limit == 0:
            return
        q = make_query_from_filter(sample_filter, require_meter=False)

        if limit:
            samples = self.db.meter.find(
                q, limit=limit, sort=[("timestamp", pymongo.DESCENDING)])
        else:
            samples = self.db.meter.find(
                q, sort=[("timestamp", pymongo.DESCENDING)])

        for s in samples:
            # Remove the ObjectId generated by the database when
            # the sample was inserted. It is an implementation
            # detail that should not leak outside of the driver.
            del s['_id']
            # Backward compatibility for samples without units
            s['counter_unit'] = s.get('counter_unit', '')
            yield models.Sample(**s)

    def get_meter_statistics(self, sample_filter, period=None, groupby=None):
        """Return an iterable of models.Statistics instance containing meter
        statistics described by the query parameters.

        The filter must have a meter value set.

        """
        #FIXME(sileht): since testscenarios is used
        # all API functionnal and DB tests have been enabled
        # get_meter_statistics will not return the expected data in some tests
        # Some other tests return "IndexError: list index out of range"
        # on the line: rslt = results['result'][0]
        # complete trace: http://paste.openstack.org/show/45016/
        # And because I have no db2 installation to test,
        # I have disable this method until it is fixed
        raise NotImplementedError("Statistics not implemented")

        if groupby:
            raise NotImplementedError("Group by not implemented.")

        q = make_query_from_filter(sample_filter)

        if period:
            raise NotImplementedError('Statistics for period not implemented.')

        results = self.db.meter.aggregate([
            {'$match': q},
            {'$group': self.GROUP},
            {'$project': self.PROJECT},
        ])

        # Since there is no period grouping, there should be only one set in
        # the results
        rslt = results['result'][0]

        duration = rslt['duration_end'] - rslt['duration_start']
        if hasattr(duration, 'total_seconds'):
            rslt['duration'] = duration.total_seconds()
        else:
            rslt['duration'] = duration.days * 3600 + duration.seconds

        rslt['period_start'] = rslt['duration_start']
        rslt['period_end'] = rslt['duration_end']
        # Period is not supported, set it to zero
        rslt['period'] = 0
        rslt['groupby'] = None

        return [models.Statistics(**(rslt))]

    @staticmethod
    def _decode_matching_metadata(matching_metadata):
        if isinstance(matching_metadata, dict):
            #note(sileht): keep compatibility with old db format
            return matching_metadata
        else:
            new_matching_metadata = {}
            for elem in matching_metadata:
                new_matching_metadata[elem['key']] = elem['value']
            return new_matching_metadata

    @staticmethod
    def _encode_matching_metadata(matching_metadata):
        if matching_metadata:
            new_matching_metadata = []
            for k, v in matching_metadata.iteritems():
                new_matching_metadata.append({'key': k, 'value': v})
            return new_matching_metadata
        return matching_metadata

    def get_alarms(self, name=None, user=None,
                   project=None, enabled=True, alarm_id=None, pagination=None):
        """Yields a lists of alarms that match filters
        :param user: Optional ID for user that owns the resource.
        :param project: Optional ID for project that owns the resource.
        :param enabled: Optional boolean to list disable alarm.
        :param alarm_id: Optional alarm_id to return one alarm.
        :param metaquery: Optional dict with metadata to match on.
        :param resource: Optional resource filter.
        :param pagination: Optional pagination query.
        """

        if pagination:
            raise NotImplementedError(_('Pagination not implemented'))

        q = {}
        if user is not None:
            q['user_id'] = user
        if project is not None:
            q['project_id'] = project
        if name is not None:
            q['name'] = name
        if enabled is not None:
            q['enabled'] = enabled
        if alarm_id is not None:
            q['alarm_id'] = alarm_id

        for alarm in self.db.alarm.find(q):
            a = {}
            a.update(alarm)
            del a['_id']
            a['matching_metadata'] = \
                self._decode_matching_metadata(a['matching_metadata'])
            yield models.Alarm(**a)

    def update_alarm(self, alarm):
        """update alarm
        """
        data = alarm.as_dict()
        data['matching_metadata'] = \
            self._encode_matching_metadata(data['matching_metadata'])

        self.db.alarm.update(
            {'alarm_id': alarm.alarm_id},
            {'$set': data},
            upsert=True)

        stored_alarm = self.db.alarm.find({'alarm_id': alarm.alarm_id})[0]
        del stored_alarm['_id']
        stored_alarm['matching_metadata'] = \
            self._decode_matching_metadata(stored_alarm['matching_metadata'])
        return models.Alarm(**stored_alarm)

    create_alarm = update_alarm

    def delete_alarm(self, alarm_id):
        """Delete an alarm
        """
        self.db.alarm.remove({'alarm_id': alarm_id})

    def get_alarm_changes(self, alarm_id, on_behalf_of):
        """Yields list of AlarmChanges describing alarm history
        :param alarm_id: ID of alarm to return changes for
        :param on_behalf_of: ID of tenant to scope changes query (None for
                             administrative user, indicating all projects)
        """
        raise NotImplementedError('Alarm history not implemented')

    def record_alarm_change(self, alarm_change):
        """Record alarm change event.
        """
        raise NotImplementedError('Alarm history not implemented')

    @staticmethod
    def record_events(events):
        """Write the events.

        :param events: a list of model.Event objects.
        """
        raise NotImplementedError('Events not implemented.')

    @staticmethod
    def get_events(event_filter):
        """Return an iterable of model.Event objects.

        :param event_filter: EventFilter instance
        """
        raise NotImplementedError('Events not implemented.')
