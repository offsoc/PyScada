# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import models
from django.db.utils import IntegrityError
from django.contrib.auth.models import User
from django.conf import settings

from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.utils.timezone import now, make_aware, is_naive
from django.db.models.signals import post_save
from django.db.models.fields.related import OneToOneRel

from pyscada.utils import blow_up_data, timestamp_to_datetime, min_pass, max_pass
from pyscada.utils import _get_objects_for_html as get_objects_for_html

from six import text_type
import traceback
import time
import datetime
import json
import signal
from monthdelta import monthdelta
from os import kill, waitpid, WNOHANG
from struct import *
from os import getpid
from dateutil import relativedelta
import errno
import numpy as np
import logging

logger = logging.getLogger(__name__)


try:
    import channels.layers
    from channels.exceptions import InvalidChannelLayerError
    from channels.exceptions import ChannelFull
    from redis.exceptions import ConnectionError
    from asgiref.sync import async_to_sync
    from asyncio import wait_for
    try:
        from asyncio.exceptions import TimeoutError as asyncioTimeoutError
        from asyncio.exceptions import CancelledError as asyncioCancelledError
    except ModuleNotFoundError:
        # for python version < 3.8
        from asyncio import TimeoutError as asyncioTimeoutError
        from asyncio import CancelledError as asyncioCancelledError
    if channels.layers.get_channel_layer() is None:
        logger.warning("Django Channels is not working. Missing config in settings ?")
        channels_driver = False
    else:
        try:
            async def channels_test():
                await wait_for(channels.layers.get_channel_layer().receive('test'), timeout=0.1)
            async_to_sync(channels_test)()
            channels_driver = True
        except ConnectionError as e:
            # Redis service failed to start
            logger.warning("Redis service failed to start. %s" % e)
            channels_driver = False
except (ImportError, ModuleNotFoundError):
    channels_driver = False
except ConnectionRefusedError:
    logger.warning("Django Channels is not working. redis-server not running ?")
    channels_driver = False
except (TimeoutError, asyncioTimeoutError):
    channels_driver = True


#
# Manager
#
class RecordedDataValueManager(models.Manager):
    def filter_time(self, time_min=None, time_max=None, use_date_saved=True, **kwargs):
        if time_min is None:
            time_min = 0

        if time_max is None:
            time_max = time.time()
        if use_date_saved:
            return super(RecordedDataValueManager, self).get_queryset().filter(
                date_saved__range=(timestamp_to_datetime(time_min), timestamp_to_datetime(time_max)), **kwargs)
        else:
            return super(RecordedDataValueManager, self).get_queryset().filter(
                id__range=(time_min * 2097152 * 1000, time_max * 2097152 * 1000 + 2097151), **kwargs)

    def last_element(self, use_date_saved=True, **kwargs):

        if 'time_min' in kwargs:
            time_min = kwargs.pop('time_min')
        else:
            time_min = (time.time() - 3660)

        if 'time_max' in kwargs:
            time_max = kwargs.pop('time_max')
        else:
            time_max = time.time()
        if use_date_saved:
            return super(RecordedDataValueManager, self).get_queryset().filter(
                date_saved__range=(timestamp_to_datetime(time_min), timestamp_to_datetime(time_max)), **kwargs).last()
        else:
            return super(RecordedDataValueManager, self).get_queryset().filter(
                id__range=(time_min * 2097152 * 1000, time_max * 2097152 * 1000 + 2097151), **kwargs).last()

    def get_values_in_time_range(self, time_min=None, time_max=None, query_first_value=False, time_in_ms=False,
                                 key_is_variable_name=False, add_timestamp_field=False, add_fake_data=False,
                                 add_latest_value=True, blow_up=False, use_date_saved=False,
                                 use_recorded_data_old=False, add_date_saved_max_field=False, **kwargs):
        # logger.debug('%r' % [time_min, time_max])
        if time_min is None:
            time_min = 0
        else:
            db_time_min = RecordedData.objects.first()
            if use_recorded_data_old:
                pass  # todo
            if db_time_min:
                db_time_min = db_time_min.timestamp
            else:
                return None
            time_min = max(db_time_min, time_min)
        if time_max is None:
            time_max = time.time()
        else:
            time_max = min(time_max, time.time())

        # logger.debug('%r' % [time_min, time_max])
        date_saved_max = time_min
        values = {}
        var_filter = True
        if 'variable' in kwargs:
            variables = Variable.objects.filter(pk=kwargs['variable'].pk)
        elif 'variable_id' in kwargs:
            variables = Variable.objects.filter(pk=kwargs['variable_id'])
        elif 'variable_pk__in' in kwargs:
            # return all values for the given variables
            variables = Variable.objects.filter(pk__in=kwargs['variable_pk__in'])
        elif 'variable_id__in' in kwargs:
            # return all values for the given variables
            variables = Variable.objects.filter(pk__in=kwargs['variable_id__in'])
        elif 'variable__in' in kwargs:
            # return all values for the given variables
            variables = kwargs['variable__in']
        else:
            variables = Variable.objects.all()
            var_filter = False

        # export in seconds or millis
        if time_in_ms:
            f_time_scale = 1000
        else:
            f_time_scale = 1

        variable_ids = variables.values_list('pk', flat=True)
        # only filter by variable wenn less the 70% of all variables are queried
        if len(variable_ids) > float(Variable.objects.count()) * 0.7:
            var_filter = False

        tmp_time_max = 0  # get the most recent time value
        tmp_time_min = time.time()  #

        time_slice = 60 * max(60, min(24 * 60, -3 * len(variable_ids) + 1440))
        query_time_min = time_min
        query_time_max = min(time_min + time_slice, time_max)
        # logger.debug('%r'%[time_min,time_max,query_time_min,query_time_max,time_slice])

        while query_time_min < time_max:
            if use_date_saved:
                if var_filter:
                    tmp = list(super(RecordedDataValueManager, self).get_queryset().filter(
                        date_saved__range=(timestamp_to_datetime(query_time_min),
                                           timestamp_to_datetime(min(query_time_max, time_max))),
                        variable__in=variables
                    ).values_list('variable_id', 'pk', 'value_float64',
                                  'value_int64', 'value_int32', 'value_int16',
                                  'value_boolean', 'date_saved'))
                else:
                    tmp = list(super(RecordedDataValueManager, self).get_queryset().filter(
                        date_saved__range=(timestamp_to_datetime(query_time_min),
                                           timestamp_to_datetime(min(query_time_max, time_max)))
                    ).values_list('variable_id', 'pk', 'value_float64',
                                  'value_int64', 'value_int32', 'value_int16', 'value_boolean', 'date_saved'))
            else:
                if var_filter:
                    tmp = list(super(RecordedDataValueManager, self).get_queryset().filter(
                        id__range=(query_time_min * 2097152 * 1000, min(query_time_max * 2097152 * 1000 + 2097151,
                                                                        time_max * 2097152 * 1000 + 2097151)),
                        variable__in=variables
                    ).values_list('variable_id', 'pk', 'value_float64',
                                  'value_int64', 'value_int32', 'value_int16',
                                  'value_boolean', 'date_saved'))
                else:
                    tmp = list(super(RecordedDataValueManager, self).get_queryset().filter(
                        id__range=(query_time_min * 2097152 * 1000, min(query_time_max * 2097152 * 1000 + 2097151,
                                                                        time_max * 2097152 * 1000 + 2097151))
                    ).values_list('variable_id', 'pk', 'value_float64',
                                  'value_int64', 'value_int32', 'value_int16', 'value_boolean', 'date_saved'))

            for item in tmp:
                if item[0] not in variable_ids:
                    continue
                if not item[0] in values:
                    values[item[0]] = []
                tmp_time = float(item[1] - item[0]) / (2097152.0 * 1000)  # calc the timestamp in seconds
                tmp_time_max = max(tmp_time, tmp_time_max)
                tmp_time_min = min(tmp_time, tmp_time_min)
                tmp_time = tmp_time * f_time_scale
                date_saved_max = max(date_saved_max, time.mktime(item[7].utctimetuple())+item[7].microsecond/1e6)
                if item[2] is not None:  # float64
                    values[item[0]].append([tmp_time, item[2]])  # time, value
                elif item[3] is not None:  # int64
                    values[item[0]].append([tmp_time, item[3]])  # time, value
                elif item[4] is not None:  # int32
                    values[item[0]].append([tmp_time, item[4]])  # time, value
                elif item[5] is not None:  # int16
                    values[item[0]].append([tmp_time, item[5]])  # time, value
                elif item[6] is not None:  # boolean
                    values[item[0]].append([tmp_time, item[6]])  # time, value
                else:
                    values[item[0]].append([tmp_time, 0])  # time, value

            del tmp
            query_time_min = query_time_max  # + 1
            query_time_max = query_time_min + time_slice

        update_first_value_list = []
        timestamp_max = tmp_time_max
        for key, item in values.items():
            if item[-1][0] < time_max * f_time_scale:
                if (time_max * f_time_scale) - item[-1][0] < 3610 and add_latest_value:
                    # append last value
                    item.append([time_max * f_time_scale, item[-1][1]])

            if query_first_value and item[0][0] > time_min * f_time_scale:
                update_first_value_list.append(key)

        if query_first_value:
            for vid in variable_ids:
                if vid not in values.keys():
                    update_first_value_list.append(vid)

        if len(update_first_value_list) > 0:  # TODO add n times the recording interval to the range (3600 + n)
            if use_date_saved:
                tmp = list(super(RecordedDataValueManager, self).get_queryset().filter(
                    use_date_saved__range=(timestamp_to_datetime(time_min - 3660), timestamp_to_datetime(time_min)),
                    variable_id__in=update_first_value_list
                ).values_list('variable_id', 'pk', 'value_float64',
                              'value_int64', 'value_int32', 'value_int16',
                              'value_boolean'))
            else:
                tmp = list(super(RecordedDataValueManager, self).get_queryset().filter(
                    id__range=((time_min - 3660) * 2097152 * 1000, time_min * 2097152 * 1000),
                    variable_id__in=update_first_value_list
                ).values_list('variable_id', 'pk', 'value_float64',
                              'value_int64', 'value_int32', 'value_int16',
                              'value_boolean'))

            first_values = {}
            for item in tmp:
                tmp_timestamp = float(item[1] - item[0]) / (2097152.0 * 1000)

                if not item[0] in first_values:
                    first_values[item[0]] = [tmp_timestamp, 0]

                if tmp_timestamp >= first_values[item[0]][0]:
                    if item[2] is not None:  # float64
                        first_values[item[0]][1] = item[2]  # time, value
                    elif item[3] is not None:  # int64
                        first_values[item[0]][1] = item[3]  # time, value
                    elif item[4] is not None:  # int32
                        first_values[item[0]][1] = item[4]  # time, value
                    elif item[5] is not None:  # int16
                        first_values[item[0]][1] = item[5]  # time, value
                    elif item[6] is not None:  # boolean
                        first_values[item[0]][1] = item[6]  # time, value

            for key in update_first_value_list:
                if key in first_values:
                    if key not in values:
                        values[key] = []
                    values[key].insert(0, [time_min * f_time_scale, first_values[key][1]])

        '''
        add a data point before the next change of state
        '''
        if add_fake_data:
            for key in values:
                i = 1
                while i < len(values[key]):
                    if values[key][i][0] - values[key][i - 1][0] > 1.0 and values[key][i][1] != values[key][i - 1][1]:
                        values[key].insert(i, [values[key][i][0], values[key][i - 1][1]])
                        i += 2
                    else:
                        i += 1
        '''
        blow up data
        '''

        if blow_up:
            if 'mean_value_period' in kwargs:
                mean_value_period = kwargs['mean_value_period']
            else:
                mean_value_period = 5.0
            if 'no_mean_value' in kwargs:
                no_mean_value = kwargs['no_mean_value']
            else:
                no_mean_value = True
            timevalues = np.arange(np.ceil(time_min / mean_value_period) * mean_value_period*f_time_scale,
                                   np.floor(time_max / mean_value_period) * mean_value_period*f_time_scale,
                                   mean_value_period * f_time_scale)

            for key in values:
                values[key] = blow_up_data(values[key], timevalues, mean_value_period*f_time_scale, no_mean_value)
            values['timevalues'] = timevalues

        '''
        change output tuple key from pk to variable name
        '''
        if key_is_variable_name:
            for item in variables:
                if item.pk in values:
                    values[item.name] = values.pop(item.pk)
        '''
        add the timestamp of the most recent value
        '''
        if add_timestamp_field:
            if timestamp_max == 0:
                timestamp_max = time_min
            values['timestamp'] = timestamp_max * f_time_scale

        if add_date_saved_max_field:
            values['date_saved_max'] = date_saved_max * f_time_scale
        return values

    def db_data(self, variable_ids, time_min, time_max, time_in_ms=True, query_first_value=False):
        """

        :return:
        """

        variable_ids = [int(pk) for pk in variable_ids]
        tmp = list(super(RecordedDataValueManager, self).get_queryset().filter(
            id__range=((time_min - 3660) * 2097152 * 1000, time_max * 2097152 * 1000),
            date_saved__range=(timestamp_to_datetime(time_min - 3660 if query_first_value else time_min),
                               timestamp_to_datetime(time_max)),
            variable_id__in=variable_ids
        ).values_list('variable_id', 'pk', 'value_float64',
                      'value_int64', 'value_int32', 'value_int16',
                      'value_boolean', 'date_saved'))

        if time_in_ms:
            f_time_scale = 1000
        else:
            f_time_scale = 1

        values = dict()
        times = dict()
        date_saved_max = 0
        tmp_time_max = 0
        tmp_time_min = time_max

        def get_rd_value(rd_resp):
            # return the value from a RecordedData Response
            if rd_resp[2] is not None:  # float64
                return rd_resp[2]  # time, value
            elif rd_resp[3] is not None:  # int64
                return rd_resp[3]  # time, value
            elif rd_resp[4] is not None:  # int32
                return rd_resp[4]  # time, value
            elif rd_resp[5] is not None:  # int16
                return rd_resp[5]  # time, value
            elif rd_resp[6] is not None:  # boolean
                return rd_resp[6]  # time, value
            else:
                return 0

        for item in tmp:
            if item[0] not in variable_ids:
                continue
            if not item[0] in values:
                values[item[0]] = []
                times[item[0]] = {'time_min': time_max, 'time_max': 0}
            tmp_time = float(item[1] - item[0]) / (2097152.0 * 1000)  # calc the timestamp in seconds
            date_saved_max = max(date_saved_max, time.mktime(item[7].utctimetuple()) + item[7].microsecond / 1e6)
            tmp_time_max = max(tmp_time, tmp_time_max)
            tmp_time_min = min(tmp_time, tmp_time_min)
            values[item[0]].append([tmp_time * f_time_scale, get_rd_value(item)])
            if tmp_time < times[item[0]]['time_min']:
                times[item[0]]['time_min'] = tmp_time
            if tmp_time > times[item[0]]['time_max']:
                times[item[0]]['time_max'] = tmp_time

        if query_first_value:
            for pk in variable_ids:
                if pk not in values:
                    values[pk] = []
                if pk in times:
                    last_element = self.last_element(use_date_saved=True, time_min=0,
                                                     time_max=times[pk]['time_min'], variable_id=pk)
                    if last_element is not None:
                        values[pk].insert(0, [(float(last_element.pk - last_element.variable_id) / (2097152.0 * 1000))
                                              * f_time_scale, last_element.value()])

        values['timestamp'] = max(tmp_time_max, time_min) * f_time_scale
        values['date_saved_max'] = date_saved_max * f_time_scale

        return values


class VariablePropertyManager(models.Manager):
    """

    """

    def update_or_create_property(self, variable, name, value, value_class='string', property_class=None,
                                  timestamp=None, **kwargs):
        """

        :param variable: Variable Object
        :param name: Property Name (DEVICE:PROPERTY_NAME)
        :param value: a value
        :param value_class: type or class of the value
        :param property_class: class of the property
        :param timestamp:
        :return: VariableProperty Object
        """
        if type(variable) == Variable:
            kwargs = {'name': name.upper(), 'variable_id': variable.pk}
        elif type(variable) == int or type(variable) == float:
            kwargs = {'name': name.upper(), 'variable_id': variable}
        else:
            logger.debug("update_or_create_property failed with variable : " + str(variable) +
                         " - and property name : " + str(name))
            return None

        vp = super(VariablePropertyManager, self).get_queryset().filter(**kwargs).first()
        kwargs['value_class'] = value_class.upper()
        if timestamp is not None:
            kwargs['timestamp'] = timestamp
        if property_class is not None:
            kwargs['property_class'] = property_class
        if value_class.upper() in ['STRING']:
            kwargs['value_string'] = str(value)[:VariableProperty._meta.get_field('value_string').max_length]
        elif value_class.upper() in ['FLOAT', 'FLOAT64', 'DOUBLE', 'FLOAT32', 'SINGLE', 'REAL']:
            kwargs['value_float64'] = value
        elif value_class.upper() in ['INT64', 'UINT32', 'DWORD']:
            kwargs['value_int64'] = value
        elif value_class.upper() in ['WORD', 'UINT', 'UINT16', 'INT32']:
            kwargs['value_int32'] = value
        elif value_class.upper() in ['INT16', 'INT8', 'UINT8']:
            kwargs['value_int16'] = value
        elif value_class.upper() in ['BOOL', 'BOOLEAN']:
            kwargs['value_boolean'] = value
        if vp:
            # update
            for key, value in kwargs.items():
                setattr(vp, key, value)
            vp.save()
        else:
            # create
            vp = VariableProperty(**kwargs)
            vp.save()

        return vp

    def get_property(self, variable, name, **kwargs):
        if type(variable) == Variable:
            vp = super(VariablePropertyManager, self).get_queryset().filter(variable_id=variable.pk,
                                                                            name=name.upper(), **kwargs).first()
        elif type(variable) == int or type(variable) == float:
            vp = super(VariablePropertyManager, self).get_queryset().filter(variable_id=variable,
                                                                            name=name.upper(), **kwargs).first()
        else:
            return None
        if vp:
            return vp
        else:
            return None

    def update_property(self, variable_property=None, variable=None, name=None, value=None, **kwargs):
        if type(variable_property) == VariableProperty:
            vp = super(VariablePropertyManager, self).get_queryset().filter(pk=variable_property.pk
                                                                            ).first()
        elif type(variable_property) == int or type(variable_property) == float:
            vp = super(VariablePropertyManager, self).get_queryset().filter(pk=variable_property
                                                                            ).first()
        elif type(variable) == Variable:
            vp = super(VariablePropertyManager, self).get_queryset().filter(variable_id=variable.pk,
                                                                            name=name.upper(), **kwargs).first()
        elif type(variable) == int or type(variable) == float:
            vp = super(VariablePropertyManager, self).get_queryset().filter(variable_id=variable,
                                                                            name=name.upper(), **kwargs).first()
        else:
            return None
        if vp:
            value_class = vp.value_class
            if value_class.upper() in ['STRING']:
                value = "" if value is None else value
                vp.value_string = value[:VariableProperty._meta.get_field('value_string').max_length]
            elif value_class.upper() in ['FLOAT', 'FLOAT64', 'DOUBLE', 'FLOAT32', 'SINGLE', 'REAL']:
                vp.value_float64 = value
            elif value_class.upper() in ['INT64', 'UINT32', 'DWORD']:
                vp.value_int64 = value
            elif value_class.upper() in ['WORD', 'UINT', 'UINT16', 'INT32']:
                vp.value_int32 = value
            elif value_class.upper() in ['INT16', 'INT8', 'UINT8']:
                vp.value_int16 = value
            elif value_class.upper() in ['BOOL', 'BOOLEAN']:
                value = False if value is None else value
                vp.value_boolean = value
            vp.last_modified = now()
            try:
                vp.save()
            except ValueError as e:
                logger.error("Error while saving VP value : " + str(e))
            return vp
        else:
            return None


#
# Models
#
class Color(models.Model):
    id = models.AutoField(primary_key=True)
    name = models.SlugField(max_length=80, verbose_name="variable name")
    R = models.PositiveSmallIntegerField(default=0)
    G = models.PositiveSmallIntegerField(default=0)
    B = models.PositiveSmallIntegerField(default=0)

    def __str__(self):
        return 'rgb(' + str(self.R) + ', ' + str(self.G) + ', ' + str(self.B) + ', ' + ')'

    def color_code(self):
        return '#%02x%02x%02x' % (self.R, self.G, self.B)

    def color_rect_html(self):
        return '<div style="width:4px;height:0;border:5px solid #%02x%02x%02x;overflow:hidden"></div>' % (
            self.R, self.G, self.B)


class DeviceProtocol(models.Model):
    id = models.AutoField(primary_key=True)
    protocol = models.CharField(max_length=400, default='generic')
    description = models.TextField(default='', verbose_name="Description", null=True)
    app_name = models.CharField(max_length=400, default='pyscada.PROTOCOL')
    device_class = models.CharField(max_length=400, default='pyscada.PROTOCOL.device')
    daq_daemon = models.BooleanField()
    single_thread = models.BooleanField()

    def __str__(self):
        return self.protocol


class DeviceHandler(models.Model):
    name = models.CharField(default='', max_length=255)
    handler_class = models.CharField(default='pyscada.visa.devices.HP3456A', max_length=255,
                                     help_text='a Base class to extend can be found at '
                                               'pyscada.PROTOCOL.devices.GenericDevice. '
                                               'Exemple : pyscada.visa.devices.HP3456A, '
                                               'pyscada.smbus.devices.ups_pico, '
                                               'pyscada.serial.devices.AirLinkGX450')
    handler_path = models.CharField(default=None, max_length=255, null=True, blank=True,
                                    help_text='If no handler class, pyscada will look at the path. '
                                              'Exemple : /home/pi/my_handler.py')

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        # TODO : select only devices of selected variables
        post_save.send_robust(sender=DeviceHandler, instance=Device.objects.first())
        super(DeviceHandler, self).save(*args, **kwargs)


class Device(models.Model):
    id = models.AutoField(primary_key=True)
    short_name = models.CharField(max_length=400, default='')
    description = models.TextField(default='', verbose_name="Description", null=True)
    active = models.BooleanField(default=True)
    byte_order_choices = (
        ('1-0-3-2', '1-0-3-2'),
        ('0-1-2-3', '0-1-2-3'),
        ('2-3-0-1', '2-3-0-1'),
        ('3-2-1-0', '3-2-1-0'),
    )
    byte_order = models.CharField(max_length=15, default='1-0-3-2', choices=byte_order_choices)
    polling_interval_choices = (
        (0.1, '100 Milliseconds'),
        (0.5, '500 Milliseconds'),
        (1.0, '1 Second'),
        (5.0, '5 Seconds'),
        (10.0, '10 Seconds'),
        (15.0, '15 Seconds'),
        (30.0, '30 Seconds'),
        (60.0, '1 Minute'),
        (150.0, '2.5 Mintues'),
        (300.0, '5 Minutes'),
        (360.0, '6 Minutes (10 times per Hour)'),
        (600.0, '10 Minutes'),
        (900.0, '15 Minutes'),
        (1800.0, '30 Minutes'),
        (3600.0, '1 Hour'),
        (21600.0, '6 Hours'),
        (43200.0, '12 Hours'),
        (86400.0, '1 Day'),
        (604800.0, '1 Week'),
    )
    polling_interval = models.FloatField(default=polling_interval_choices[3][0], choices=polling_interval_choices)
    protocol = models.ForeignKey(DeviceProtocol, null=True, on_delete=models.CASCADE)
    instrument_handler = models.ForeignKey(DeviceHandler, null=True, blank=True, on_delete=models.SET_NULL)

    def __str__(self):
        # display protocol for the JS filter for inline variables (hmi.static.pyscada.js.admin)
        if self.protocol is not None:
            return self.protocol.protocol + "-" + self.short_name
        else:
            return 'generic-' + self.short_name

    def get_device_instance(self):
        try:
            mod = __import__(self.protocol.device_class, fromlist=['Device'])
            device_class = getattr(mod, 'Device')
            return device_class(self)
        except:
            logger.error('%s(%d), unhandled exception\n%s' % (self.short_name, getpid(), traceback.format_exc()))
            return None


class Unit(models.Model):
    id = models.AutoField(primary_key=True)
    unit = models.CharField(max_length=80, verbose_name="Unit")
    description = models.TextField(default='', verbose_name="Description", null=True)
    udunit = models.CharField(max_length=500, verbose_name="udUnit", default='')

    def __str__(self):
        return self.unit

    class Meta:
        managed = True


class Scaling(models.Model):
    id = models.AutoField(primary_key=True)
    description = models.TextField(default='', verbose_name="Description", null=True, blank=True)
    input_low = models.FloatField()
    input_high = models.FloatField()
    output_low = models.FloatField()
    output_high = models.FloatField()
    limit_input = models.BooleanField()

    def __str__(self):
        if self.description:
            return self.description
        else:
            return str(self.id) + '_[' + str(self.input_low) + ':' + \
                   str(self.input_high) + '] -> [' + str(self.output_low) + ':' \
                   + str(self.output_low) + ']'

    def scale_value(self, input_value):
        input_value = float(input_value)
        if self.limit_input:
            input_value = max(min(input_value, self.input_high), self.input_low)
        norm_value = (input_value - self.input_low) / (self.input_high - self.input_low)
        return norm_value * (self.output_high - self.output_low) + self.output_low

    def scale_output_value(self, input_value):
        input_value = float(input_value)
        norm_value = (input_value - self.output_low) / (self.output_high - self.output_low)
        return norm_value * (self.input_high - self.input_low) + self.input_low


class Dictionary(models.Model):
    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=400, default='')

    def __str__(self):
        return text_type(str(self.id) + ': ' + self.name)

    def dict_as_json(self):
        items_list = dict()
        for item in self.dictionaryitem_set.all():
            items_list[float(item.value)] = item.label
        return json.dumps(items_list)

    def get_label(self, value):
        label_found = None
        for item in self.dictionaryitem_set.all():
            if float(item.value) == float(value):
                if label_found is None:
                    label_found = item.label
                else:
                    logger.info('Dictionary %s has various items with value = %s' % (str(self), value))
                    return None
        return label_found or value

    def append(self, label, value, silent=False):
        try:
            DictionaryItem.objects.get(label=label, value=value, dictionary=self)
            if not silent:
                logger.warning('Item ({}:{}) for dictionary {} already exist'.format(label, value, self))
        except DictionaryItem.DoesNotExist:
            di = DictionaryItem(label=label, value=value, dictionary=self)
            di.save()

    def remove(self, label=None, value=None):
        if label is not None and value is not None:
            DictionaryItem.objects.filter(label=label, value=value).delete()
        elif label is not None:
            DictionaryItem.objects.filter(label=label).delete()
        elif value is not None:
            DictionaryItem.objects.filter(value=value).delete()


class DictionaryItem(models.Model):
    id = models.AutoField(primary_key=True)
    label = models.CharField(max_length=400, default='', blank=True)
    value = models.CharField(max_length=400, default='')
    dictionary = models.ForeignKey(Dictionary, blank=True, null=True, on_delete=models.CASCADE)

    def __str__(self):
        return text_type(str(self.id) + ': ' + self.label)


class VariableProperty(models.Model):
    id = models.AutoField(primary_key=True)
    variable = models.ForeignKey('Variable', null=True, on_delete=models.CASCADE)
    property_class_choices = ((None, 'other or no Class specified'),
                              ('device', 'Device Property'),
                              ('data_record', 'Recorded Data'),
                              ('daemon', 'Daemon Property'),
                              )
    property_class = models.CharField(default=None, blank=True, null=True, max_length=255,
                                      choices=property_class_choices)
    value_class_choices = (('FLOAT32', 'REAL (FLOAT32)'),
                           ('FLOAT32', 'SINGLE (FLOAT32)'),
                           ('FLOAT32', 'FLOAT32'),
                           ('UNIXTIMEF32', 'UNIXTIMEF32'),
                           ('FLOAT64', 'LREAL (FLOAT64)'),
                           ('FLOAT64', 'FLOAT  (FLOAT64)'),
                           ('FLOAT64', 'DOUBLE (FLOAT64)'),
                           ('FLOAT64', 'FLOAT64'),
                           ('UNIXTIMEF64', 'UNIXTIMEF64'),
                           ('INT64', 'INT64'),
                           ('UINT64', 'UINT64'),
                           ('UNIXTIMEI64', 'UNIXTIMEI64'),
                           ('UNIXTIMEI32', 'UNIXTIMEI32'),
                           ('INT32', 'INT32'),
                           ('UINT32', 'DWORD (UINT32)'),
                           ('UINT32', 'UINT32'),
                           ('INT16', 'INT (INT16)'),
                           ('INT16', 'INT16'),
                           ('UINT16', 'WORD (UINT16)'),
                           ('UINT16', 'UINT (UINT16)'),
                           ('UINT16', 'UINT16'),
                           ('BOOLEAN', 'BOOL (BOOLEAN)'),
                           ('BOOLEAN', 'BOOLEAN'),
                           ('STRING', 'STRING'),
                           )
    value_class = models.CharField(max_length=15, default='FLOAT64', verbose_name="value_class",
                                   choices=value_class_choices)
    name = models.CharField(default='', blank=True, max_length=255)
    value_boolean = models.BooleanField(default=False, blank=True)  # boolean
    value_int16 = models.SmallIntegerField(null=True, blank=True)  # int16, uint8, int8
    value_int32 = models.IntegerField(null=True, blank=True)  # uint8, int16, uint16, int32
    value_int64 = models.BigIntegerField(null=True, blank=True)  # uint32, int64
    value_float64 = models.FloatField(null=True, blank=True)  # float64
    value_string = models.CharField(default='', blank=True, max_length=1000)
    timestamp = models.DateTimeField(blank=True, null=True)
    unit = models.ForeignKey(Unit, on_delete=models.SET(1), blank=True, null=True)
    objects = VariablePropertyManager()
    value_min = models.FloatField(null=True, blank=True)
    value_max = models.FloatField(null=True, blank=True)
    min_type_choices = (('lte', '<='),
                        ('lt', '<'),
                        )
    max_type_choices = (('gte', '>='),
                        ('gt', '>'),
                        )
    min_type = models.CharField(max_length=4, default='lte', choices=min_type_choices)
    max_type = models.CharField(max_length=4, default='gte', choices=max_type_choices)
    last_modified = models.DateTimeField(auto_now=True)
    dictionary = models.ForeignKey(Dictionary, blank=True, null=True, on_delete=models.SET_NULL)

    last_value = None
    value_changed = False

    class Meta:
        verbose_name_plural = "variable properties"

    def __str__(self):
        return self.get_property_class_display() + ': ' + self.name

    def value(self):
        value_class = self.value_class
        if value_class.upper() in ['STRING']:
            return self.value_string
        elif value_class.upper() in ['FLOAT', 'FLOAT64', 'DOUBLE', 'FLOAT32', 'SINGLE', 'REAL']:
            return self.value_float64
        elif value_class.upper() in ['INT64', 'UINT32', 'DWORD']:
            return self.value_int64
        elif value_class.upper() in ['WORD', 'UINT', 'UINT16', 'INT32']:
            return self.value_int32
        elif value_class.upper() in ['INT16', 'INT8', 'UINT8']:
            return self.value_int16
        elif value_class.upper() in ['BOOL', 'BOOLEAN']:
            return self.value_boolean
        return None

    def web_key(self):
        return '%d-%s' % (self.variable.pk, self.name.upper().replace(':', '-'))

    def item_type(self):
        return "variable_property"

    def convert_string_value(self, value):
        if self.dictionary is None:
            d = Dictionary(name=str(self.name) + '_auto_created')
            d.save()
            self.dictionary = d
            Variable.objects.bulk_update([self], ['dictionary'])
            self.refresh_from_db()
        if not len(self.dictionary.dictionaryitem_set.filter(label=str(value))):
            max_value = 0
            for di in self.dictionary.dictionaryitem_set.all():
                max_value = max(float(max_value), float(di.value))
            DictionaryItem(label=str(value), value=int(max_value) + 1, dictionary=self.dictionary).save()
            #logger.debug('new value : %s' % (int(max_value) + 1))
            return int(max_value) + 1
        elif len(self.dictionary.dictionaryitem_set.filter(label=str(value))) == 1:
            #logger.debug('value found : %s' % self.dictionary.dictionaryitem_set.get(label=str(value)).value)
            return float(self.dictionary.dictionaryitem_set.get(label=str(value)).value)
        else:
            logger.warning('%s duplicate values found of %s in dictionary %s' %
                           (len(self.dictionary.dictionaryitem_set.filter(label=str(value))), value, self.dictionary))
            return float(self.dictionary.dictionaryitem_set.filter(label=str(value)).first().value)


class Variable(models.Model):
    """
        Stores a variable entry, related to :mod:`pyscada.Device`,
        :mod:`pyscada.Unit`, (optional) :mod:`pyscada.Scaling`,
        (optional) :mod:`pyscada.Color` and (optional) :mod:`pyscada.Dictionary`.
    """
    id = models.AutoField(primary_key=True)
    name = models.SlugField(max_length=200, verbose_name="variable name", unique=True)
    description = models.TextField(default='', verbose_name="Description")
    device = models.ForeignKey(Device, null=True, on_delete=models.CASCADE)
    active = models.BooleanField(default=True)
    unit = models.ForeignKey(Unit, on_delete=models.SET(1))
    writeable = models.BooleanField(default=False)
    value_class_choices = (('FLOAT32', 'REAL (FLOAT32)'),
                           ('FLOAT32', 'SINGLE (FLOAT32)'),
                           ('FLOAT32', 'FLOAT32'),
                           ('UNIXTIMEF32', 'UNIXTIMEF32'),
                           ('FLOAT64', 'LREAL (FLOAT64)'),
                           ('FLOAT64', 'FLOAT  (FLOAT64)'),
                           ('FLOAT64', 'DOUBLE (FLOAT64)'),
                           ('FLOAT64', 'FLOAT64'),
                           ('UNIXTIMEF64', 'UNIXTIMEF64'),
                           ('FLOAT48', 'FLOAT48'),
                           ('INT64', 'INT64'),
                           ('UINT64', 'UINT64'),
                           ('UNIXTIMEI64', 'UNIXTIMEI64'),
                           ('INT48', 'INT48'),
                           ('UNIXTIMEI32', 'UNIXTIMEI32'),
                           ('INT32', 'INT32'),
                           ('UINT32', 'DWORD (UINT32)'),
                           ('UINT32', 'UINT32'),
                           ('INT16', 'INT (INT16)'),
                           ('INT16', 'INT16'),
                           ('UINT16', 'WORD (UINT16)'),
                           ('UINT16', 'UINT (UINT16)'),
                           ('UINT16', 'UINT16'),
                           ('INT8', 'INT8'),
                           ('UINT8', 'UINT8'),
                           ('BOOLEAN', 'BOOL (BOOLEAN)'),
                           ('BOOLEAN', 'BOOLEAN'),
                           )
    scaling = models.ForeignKey(Scaling, null=True, blank=True, on_delete=models.SET_NULL)
    value_class = models.CharField(max_length=15, default='FLOAT64', verbose_name="value_class",
                                   choices=value_class_choices)
    cov_increment = models.FloatField(default=0, verbose_name="COV")
    byte_order_choices = (('default', 'default (specified by device byte order)',),
                          ('1-0-3-2', '1-0-3-2'),
                          ('0-1-2-3', '0-1-2-3'),
                          ('2-3-0-1', '2-3-0-1'),
                          ('3-2-1-0', '3-2-1-0'),
                          )
    short_name = models.CharField(default='', max_length=80, verbose_name="variable short name", blank=True)
    chart_line_color = models.ForeignKey(Color, null=True, default=None, blank=True, on_delete=models.SET_NULL)
    chart_line_thickness_choices = ((3, '3Px'),)
    chart_line_thickness = models.PositiveSmallIntegerField(default=3, choices=chart_line_thickness_choices)
    value_min = models.FloatField(null=True, blank=True)
    value_max = models.FloatField(null=True, blank=True)
    min_type_choices = (('lte', '<='),
                        ('lt', '<'),
                        )
    max_type_choices = (('gte', '>='),
                        ('gt', '>'),
                        )
    min_type = models.CharField(max_length=4, default='lte', choices=min_type_choices)
    max_type = models.CharField(max_length=4, default='gte', choices=max_type_choices)
    dictionary = models.ForeignKey(Dictionary, blank=True, null=True, on_delete=models.SET_NULL)

    def hmi_name(self):
        if self.short_name and self.short_name != '-' and self.short_name != '':
            return self.short_name
        else:
            return self.name

    def chart_line_color_code(self):
        if self.chart_line_color and self.chart_line_color.id != 1:
            return self.chart_line_color.color_code()
        else:
            c = 51
            c_id = self.pk + 1
            c = c % c_id
            while c >= 51:
                c_id = c_id - c
                c = c % c_id
            return Color.objects.get(id=c_id).color_code()

    '''
    M: Mantissa
    E: Exponent
    S: Sign
    uint 0            uint 1
    byte 0   byte 1   byte 2   byte 3
    1-0-3-2 MMMMMMMM MMMMMMMM SEEEEEEE EMMMMMMM
    0-1-2-3 MMMMMMMM MMMMMMMM EMMMMMMM SEEEEEEE
    2-3-0-1 EMMMMMMM SEEEEEEE MMMMMMMM MMMMMMMM
    3-2-1-0 SEEEEEEE EMMMMMMM MMMMMMMM MMMMMMMM
    '''

    byte_order = models.CharField(max_length=15, default='default', choices=byte_order_choices)

    # for RecodedVariable
    value = None
    prev_value = None
    store_value = False
    timestamp_old = None
    timestamp = None

    def __str__(self):
        return str(self.id) + " - " + self.name

    def add_attr(self, **kwargs):
        for key in kwargs:
            setattr(self, key, kwargs[key])

    def item_type(self):
        return "variable"

    def get_bits_by_class(self):
        """
        `BOOLEAN`							1	1/16 WORD
        `UINT8` `BYTE`						8	1/2 WORD
        `INT8`								8	1/2 WORD
        `UNT16` `WORD`						16	1 WORD
        `INT16`	`INT`						16	1 WORD
        `UINT32` `DWORD`					32	2 WORD
        `INT32`								32	2 WORD
        `FLOAT32` `REAL` `SINGLE` 			32	2 WORD
        `FLOAT48` 'INT48'                  	48	3 WORD
        `FLOAT64` `LREAL` `FLOAT` `DOUBLE`	64	4 WORD
        """
        if self.value_class.upper() in ['FLOAT64', 'DOUBLE', 'FLOAT', 'LREAL', 'UNIXTIMEI64', 'UNIXTIMEF64', 'INT64',
                                        'UINT64']:
            return 64
        if self.value_class.upper() in ['FLOAT48', 'INT48']:
            return 48
        if self.value_class.upper() in ['FLOAT32', 'SINGLE', 'INT32', 'UINT32', 'DWORD', 'BCD32', 'BCD24', 'REAL',
                                        'UNIXTIMEI32', 'UNIXTIMEF32']:
            return 32
        if self.value_class.upper() in ['INT16', 'INT', 'WORD', 'UINT', 'UINT16', 'BCD16']:
            return 16
        if self.value_class.upper() in ['INT8', 'UINT8', 'BYTE', 'BCD8']:
            return 8
        if self.value_class.upper() in ['BOOL', 'BOOLEAN']:
            return 1
        else:
            return 16

    def query_prev_value(self, time_min=None, use_protocol_variable=True):
        """
        get the last value and timestamp from the database
        """
        pv = self.get_protocol_variable()
        if use_protocol_variable and pv is not None and hasattr(pv, 'query_prev_value'):
            return pv.query_prev_value(time_min)

        time_max = time.time() * 2097152 * 1000 + 2097151
        if time_min is None:
            time_min = time_max - (3 * 3660 * 1000 * 2097152)
        val = self.recordeddata_set.filter(id__range=(time_min, time_max)).last()
        if val:
            self.prev_value = val.value()
            self.timestamp_old = val.timestamp
            return True
        else:
            return False

    def update_value(self, value=None, timestamp=None):
        """
        update the value in the instance and detect value state change
        """

        try:
            value = float(value)
        except ValueError:
            # Add string value in dictionary and replace the string by the dictionary value
            if type(value) == str:
                value = self.convert_string_value(value)
            else:
                logger.info("Value read for %s format not supported : %s" % (self, type(value)))
                value = None
        except TypeError:
            pass

        if self.scaling is None or value is None or self.value_class.upper() in ['BOOL', 'BOOLEAN']:
            self.value = value
        else:
            self.value = self.scaling.scale_value(value)
        self.timestamp = timestamp
        self.store_value = False
        if self.prev_value is None:
            # no prev value in the cache, always store the value
            self.store_value = True
            self.timestamp_old = self.timestamp
        elif self.value is None:
            # value could not be queried
            self.store_value = False
        elif abs(self.prev_value - self.value) <= self.cov_increment:
            if self.timestamp_old is None:
                self.store_value = True
                self.timestamp_old = self.timestamp
            else:
                if (self.timestamp - self.timestamp_old) >= 3600:
                    # store at least every hour one value
                    # store Value if old Value is older than 1 hour
                    self.store_value = True
                    self.timestamp_old = self.timestamp

        else:
            # value has changed
            self.store_value = True
            self.timestamp_old = self.timestamp
        self.prev_value = self.value
        return self.store_value

    def decode_value(self, value):
        #
        if self.byte_order == 'default':
            byte_order = self.device.byte_order
        else:
            byte_order = self.byte_order

        if self.value_class.upper() in ['FLOAT32', 'SINGLE', 'REAL', 'UNIXTIMEF32']:
            target_format = 'f'
            source_format = '2H'
        elif self.value_class.upper() in ['UINT32', 'DWORD', 'UNIXTIMEI32']:
            target_format = 'I'
            source_format = '2H'
        elif self.value_class.upper() in ['INT32']:
            target_format = 'i'
            source_format = '2H'
        elif self.value_class.upper() in ['FLOAT48']:
            target_format = 'f'
            source_format = '3H'
        elif self.value_class.upper() in ['INT48']:
            target_format = 'q'
            source_format = '3H'
        elif self.value_class.upper() in ['FLOAT64', 'DOUBLE', 'FLOAT', 'LREAL', 'UNIXTIMEF64']:
            target_format = 'd'
            source_format = '4H'
        elif self.value_class.upper() in ['UINT64']:
            target_format = 'Q'
            source_format = '4H'
        elif self.value_class.upper() in ['INT64', 'UNIXTIMEI64']:
            target_format = 'q'
            source_format = '4H'
        elif self.value_class.upper() in ['INT16', 'INT']:
            if byte_order in ['1-0-3-2', '3-2-1-0']:
                # only convert to from uint to int
                return unpack('h', pack('H', value[0]))[0]
            else:
                # swap bytes
                return unpack('>h', pack('<H', value[0]))[0]
        elif self.value_class.upper() in ['BCD32', 'BCD24', 'BCD16']:
            target_format = 'f'
            source_format = '2H'
            return value[0]
        else:
            return value[0]

        #
        if source_format == '2H':
            if byte_order == '1-0-3-2':
                return unpack(target_format, pack(source_format, value[0], value[1]))[0]
            if byte_order == '3-2-1-0':
                return unpack(target_format, pack(source_format, value[1], value[0]))[0]
            if byte_order == '0-1-2-3':
                return unpack(target_format, pack(source_format, unpack('>H', pack('<H', value[0]))[0],
                                                  unpack('>H', pack('<H', value[1]))[0]))[0]
            if byte_order == '2-3-0-1':
                return unpack(target_format, pack(source_format, unpack('>H', pack('<H', value[1]))[0],
                                                  unpack('>H', pack('<H', value[0]))[0]))[0]
        elif source_format == '3H':
            source_format = '4H'
            if byte_order == '1-0-3-2':
                return unpack(target_format, pack(source_format, 0, value[0], value[1], value[2]))[0]
            if byte_order == '3-2-1-0':
                return unpack(target_format, pack(source_format, value[2], value[1], value[0], 0))[0]
            if byte_order == '0-1-2-3':
                return unpack(target_format, pack(source_format, 0, unpack('>H', pack('<H', value[0]))[0],
                                                  unpack('>H', pack('<H', value[1]))[0],
                                                  unpack('>H', pack('<H', value[2]))[0]))[0]
            if byte_order == '2-3-0-1':
                return unpack(target_format, pack(source_format, 0, unpack('>H', pack('<H', value[2]))[0],
                                                  unpack('>H', pack('<H', value[1]))[0],
                                                  unpack('>H', pack('<H', value[0]))[0]))[0]
            source_format = '3H'
        else:
            if byte_order == '1-0-3-2':
                return unpack(target_format, pack(source_format, value[0], value[1], value[2], value[3]))[0]
            if byte_order == '3-2-1-0':
                return unpack(target_format, pack(source_format, value[3], value[2], value[1], value[0]))[0]
            if byte_order == '0-1-2-3':
                return unpack(target_format, pack(source_format, unpack('>H', pack('<H', value[0])),
                                                  unpack('>H', pack('<H', value[1])),
                                                  unpack('>H', pack('<H', value[2])),
                                                  unpack('>H', pack('<H', value[3]))))[0]
            if byte_order == '2-3-0-1':
                return unpack(target_format, pack(source_format, unpack('>H', pack('<H', value[3])),
                                                  unpack('>H', pack('<H', value[2])),
                                                  unpack('>H', pack('<H', value[1])),
                                                  unpack('>H', pack('<H', value[0]))))[0]

    def encode_value(self, value):
        if self.value_class.upper() in ['FLOAT32', 'SINGLE', 'REAL', 'UNIXTIMEF32']:
            source_format = 'f'
            target_format = '2H'
        elif self.value_class.upper() in ['UINT32', 'DWORD', 'UNIXTIMEI32']:
            source_format = 'I'
            target_format = '2H'
        elif self.value_class.upper() in ['INT32']:
            source_format = 'i'
            target_format = '2H'
        elif self.value_class.upper() in ['FLOAT48']:
            source_format = 'f'
            target_format = '3H'
        elif self.value_class.upper() in ['INT48']:
            source_format = 'q'
            target_format = '3H'
        elif self.value_class.upper() in ['FLOAT64', 'DOUBLE', 'FLOAT', 'LREAL', 'UNIXTIMEF64']:
            source_format = 'd'
            target_format = '4H'
        elif self.value_class.upper() in ['UINT64']:
            source_format = 'Q'
            target_format = '4H'
        elif self.value_class.upper() in ['INT64', 'UNIXTIMEI64']:
            source_format = 'q'
            target_format = '4H'

        elif self.value_class.upper() in ['BCD32', 'BCD24', 'BCD16']:
            source_format = 'f'
            target_format = '2H'
            return value[0]
        else:
            return value[0]
        output = unpack(target_format, pack(source_format, value))
        #
        if self.byte_order == 'default':
            byte_order = self.device.byte_order
        else:
            byte_order = self.byte_order
        if target_format == '2H':
            if byte_order == '1-0-3-2':
                return output
            if byte_order == '3-2-1-0':
                return [output[1], output[0]]
            if byte_order == '0-1-2-3':
                return [unpack('>H', pack('<H', output[0])), unpack('>H', pack('<H', output[1]))]
            if byte_order == '2-3-0-1':
                return [unpack('>H', pack('<H', output[1])), unpack('>H', pack('<H', output[0]))]
        elif target_format == '3H':
                if byte_order == '1-0-3-2':
                    return output
                if byte_order == '3-2-1-0':
                    return [output[2], output[1], output[0]]
                if byte_order == '0-1-2-3':
                    return [unpack('>H', pack('<H', output[0]))[0], unpack('>H', pack('<H', output[1]))[0],
                            unpack('>H', pack('<H', output[2]))[0]]
                if byte_order == '2-3-0-1':
                    return [unpack('>H', pack('<H', output[2]))[0],
                            unpack('>H', pack('<H', output[1]))[0], unpack('>H', pack('<H', output[0]))[0]]
        else:
            if byte_order == '1-0-3-2':
                return output
            if byte_order == '3-2-1-0':
                return [output[3], output[2], output[1], output[0]]
            if byte_order == '0-1-2-3':
                return [unpack('>H', pack('<H', output[0])), unpack('>H', pack('<H', output[1])),
                        unpack('>H', pack('<H', output[2])), unpack('>H', pack('<H', output[3]))]
            if byte_order == '2-3-0-1':
                return [unpack('>H', pack('<H', output[3])), unpack('>H', pack('<H', output[2])),
                        unpack('>H', pack('<H', output[1])), unpack('>H', pack('<H', output[0]))]

    def create_recorded_data_element(self):
        """
        create a new element to write to archive table
        """
        if self.store_value and self.value is not None:
            # self._send_cov_notification(self.timestamp, self.value)
            return RecordedData(timestamp=self.timestamp, variable=self, value=self.value)
        else:
            return None

    def _send_cov_notification(self, timestamp, value):
        """
        Sends a COV Notification via the Django Signal interface
        :param value:
        :return:
        """
        try:
            pass
        except:
            logger.error(
                '%s, unhandled exception in COV Receiver application\n%s' % (self.name, traceback.format_exc()))

    def convert_string_value(self, value):
        try:
            return float(value)
        except ValueError:
            if self.dictionary is None:
                d = Dictionary(name=str(self.name) + '_auto_created')
                d.save()
                self.dictionary = d
                Variable.objects.bulk_update([self], ['dictionary'])
                self.refresh_from_db()
            if not len(self.dictionary.dictionaryitem_set.filter(label=str(value))):
                max_value = 0
                for di in self.dictionary.dictionaryitem_set.all():
                    max_value = max(float(max_value), float(di.value))
                DictionaryItem(label=str(value), value=int(max_value) + 1, dictionary=self.dictionary).save()
                #logger.debug('new value : %s' % (int(max_value) + 1))
                return int(max_value) + 1
            elif len(self.dictionary.dictionaryitem_set.filter(label=str(value))) == 1:
                #logger.debug('value found : %s' % self.dictionary.dictionaryitem_set.get(label=str(value)).value)
                return float(self.dictionary.dictionaryitem_set.get(label=str(value)).value)
            else:
                logger.warning('%s duplicate values found of %s in dictionary %s' %
                               (len(self.dictionary.dictionaryitem_set.filter(label=str(value))), value, self.dictionary))
                return float(self.dictionary.dictionaryitem_set.filter(label=str(value)).first().value)

    def _get_objects_for_html(self, list_to_append=None, obj=None, exclude_model_names=None):
        list_to_append = get_objects_for_html(list_to_append, self, exclude_model_names)
        if hasattr(self, 'calculatedvariableselector'):
            list_to_append = self.calculatedvariableselector._get_objects_for_html(list_to_append, None, ['main_variable'])
        if hasattr(self, 'calculatedvariable'):
            list_to_append = get_objects_for_html(list_to_append, self.calculatedvariable, ['variable_calculated_fields', 'store_variable'])

        return list_to_append

    def get_protocol_variable(self):
        related_variables = [field for field in Variable._meta.get_fields() if issubclass(type(field), OneToOneRel)]
        for v in related_variables:
            if hasattr(self, v.name) and hasattr(getattr(self, v.name), 'protocol_id') and hasattr(self, "device") and getattr(self, v.name).protocol_id == self.device.protocol.id:
                return getattr(self, v.name)
        return None


def validate_nonzero(value):
    if value == 0:
        raise ValidationError(
            _('Quantity %(value)s is not allowed'),
            params={'value': value},
        )


def start_from_default():
    return make_aware(datetime.datetime.combine(datetime.date.today(), datetime.datetime.min.time()))


class PeriodicField(models.Model):
    """
    Auto calculate and store value related to a Variable for a time range.
    Example: - store the min of each month.
    - store difference of each day between 9am an 8:59am
    """
    type_choices = ((0, 'min'),
                    (1, 'max'),
                    (2, 'total'),
                    (3, 'difference'),
                    (4, 'difference percent'),
                    (5, 'delta'),
                    (6, 'mean'),
                    (7, 'first'),
                    (8, 'last'),
                    (9, 'count'),
                    (10, 'count value'),
                    (11, 'range'),
                    (12, 'step'),
                    (13, 'change count'),
                    (14, 'distinct count'),
                    )
    type = models.SmallIntegerField(choices=type_choices,
                                    help_text="Min: Minimum value of a field<br>"
                                              "Max: Maximum value of a field<br>"
                                              "Total: Sum of all values in a field<br>"
                                              "Difference: Difference between first and last value of a field<br>"
                                              "Difference percent: Percentage change between "
                                              "first and last value of a field<br>"
                                              "Delta: Cumulative change in value, only counts increments<br>"
                                              "Mean: Mean value of all values in a field<br>"
                                              "First: First value in a field<br>"
                                              "Last: Last value in a field<br>"
                                              "Count: Number of values in a field<br>"
                                              "Count value: Number of a value in a field<br>"
                                              "Range: Difference between maximum and minimum values of a field<br>"
                                              "Step: Minimal interval between values of a field<br>"
                                              "Change count: Number of times the field’s value changes<br>"
                                              "Distinct count: Number of unique values in a field")
    property = models.CharField(default='', blank=True, null=True,
                                max_length=255, help_text="Min: superior or equal this value, ex: 53.5 "
                                                          "(use >53.5 for strictly superior)<br>"
                                                          "Max: lower or equal this value, ex: 53.5 "
                                                          "(use <53.5 for strictly lower)<br>"
                                                          "Count value : enter the value to count")
    start_from = models.DateTimeField(default=start_from_default,
                                      help_text="Calculate from this DateTime and then each period_factor*period")
    period_choices = ((0, 'second'),
                      (1, 'minute'),
                      (2, 'hour'),
                      (3, 'day'),
                      (4, 'week'),
                      (5, 'month'),
                      (6, 'year'),
                      )
    period = models.SmallIntegerField(choices=period_choices)
    period_factor = models.PositiveSmallIntegerField(default=1, validators=[validate_nonzero],
                                                     help_text='Example: set to 2 and choose '
                                                               'minute to have a 2 minutes period')

    def __str__(self):
        s = self.type_choices[self.type][1] + "-"
        if self.property != '' and self.property is not None:
            s += str(self.property).replace('<', 'lt').replace('>', 'gt') + "-"
        s += str(self.period_factor) + self.period_choices[self.period][1]
        if self.period_factor > 1:
            s += "s"
        s += "-from:" + str(self.start_from.date()) + "T" + str(self.start_from.time())
        return s

    def validate_unique(self, exclude=None):
        qs = PeriodicField.objects.filter(type=self.type,
                                          property=self.property,
                                          start_from=self.start_from,
                                          period=self.period,
                                          period_factor=self.period_factor,
                                          ).exclude(id=self.id)
        if len(qs):
            raise ValidationError('This periodic field already exist.')


class CalculatedVariableSelector(models.Model):
    main_variable = models.OneToOneField(Variable, on_delete=models.CASCADE)
    period_fields = models.ManyToManyField(PeriodicField)
    active = models.BooleanField(default=True)
    dname = "for_calculated_variables"

    def get_new_calculated_variable(self, main_var, period):
        v = Variable.objects.get(id=main_var.id)
        try:
            d = Device.objects.get(short_name=self.dname)
        except Device.DoesNotExist:
            d = Device.objects.create(
            short_name=self.dname,
            description="Device used to store calculated variables",
            protocol_id=1)
        sv_name = v.name[:Variable._meta.get_field('name').max_length - len(str(period).replace(":", "-")) - 1] \
            + "-" + str(period).replace(":", "-")
        sv_name = sv_name[:Variable._meta.get_field('name').max_length]
        if len(Variable.objects.filter(name=sv_name)) == 0:
            v.id = None
            v.name = sv_name
            v.description = str(period)
            v.writeable = False
            v.cov_increment = -1
            v.device_id = d.id
            v.scaling = None
            v.value_class = 'FLOAT64'
            v.save()
            logger.debug("Create CalculatedVariable: " + sv_name)
            pv = CalculatedVariable(store_variable=v, variable_calculated_fields=self, period=period)
        else:
            pv = None

        return pv

    def create_all_calculated_variables(self):
        cvs = []
        self.refresh_from_db()
        for p in self.period_fields.all():
            cv = self.get_new_calculated_variable(self.main_variable, p)
            if cv is not None:
                cvs.append(cv)

        #logger.debug(cvs)
        CalculatedVariable.objects.bulk_create(cvs)

    def __str__(self):
        return self.main_variable.name

    def _get_objects_for_html(self, list_to_append=None, obj=None, exclude_model_names=None):
        list_to_append = get_objects_for_html(list_to_append, self, exclude_model_names)
        for calculatedvariable in self.calculatedvariable_set.all():
            list_to_append = get_objects_for_html(list_to_append, calculatedvariable, ['variable_calculated_fields',])

        return list_to_append


class CalculatedVariable(models.Model):
    store_variable = models.OneToOneField(Variable, on_delete=models.CASCADE)
    variable_calculated_fields = models.ForeignKey(CalculatedVariableSelector, on_delete=models.CASCADE)
    period = models.ForeignKey(PeriodicField, on_delete=models.CASCADE)
    last_check = models.DateTimeField(blank=True, null=True)
    state = models.CharField(default='', max_length=100)

    def __str__(self):
        return self.store_variable.name

    def check_to_now(self, force_write=False, add_partial_info=False):
        if self.last_check is not None:
            self.check_period(self.last_check, now(), force_write, add_partial_info)
        else:
            self.check_period(self.period.start_from, now(), force_write, add_partial_info)

    def check_period(self, d1, d2, force_write=False, add_partial_info=False):
        #logger.debug("Check period of %s [%s - %s]" % (self.store_variable, d1, d2))
        self.state = "Checking [%s to %s]" % (d1, d2)
        self.state = self.state[0:100]
        self.save(update_fields=['state'])

        if is_naive(d1):
            d1 = make_aware(d1)
        if is_naive(d2):
            d2 = make_aware(d2)
        output = []

        if self.period_diff_quantity(d1, d2) is None:
            #logger.debug("No period in date interval : %s (%s %s)" % (self.period, d1, d2))
            self.state = "[%s to %s] < %s" % (d1, d2, str(self.period.period_factor) +
                                              self.period.period_choices[self.period.period][1])
            self.state = self.state[0:100]
            self.save(update_fields=['state'])
            return None

        td = self.add_timedelta()

        d = self.get_valid_range(d1, d2)
        if d is None:
            self.state = "No time range found [%s to %s] %s" % (d1, d2, self.period)
            self.state = self.state[0:100]
            self.save(update_fields=['state'])
            return None
        [d1, d2] = d

        if self.period_diff_quantity(d1, d2) is None:
            logger.debug("No period in new date interval : %s (%s %s)" % (self.period, d1, d2))
            self.state = "[%s to %s] < %s" % (d1, d2, str(self.period.period_factor) +
                                              self.period.period_choices[self.period.period][1])
            self.state = self.state[0:100]
            self.save(update_fields=['state'])
            return None

        #logger.debug("Valid range : %s - %s" % (d1, d2))

        while d2 >= d1 + td and d1 + td <= now():
            #logger.debug("add for %s - %s" % (d1, d1 + td))
            td1 = d1.timestamp()
            try:
                v_stored = RecordedData.objects.get_values_in_time_range(time_min=td1, time_max=td1 + 1,
                                                                         variable=self.store_variable,
                                                                         add_latest_value=False)
            except AttributeError:
                v_stored = []
            if not force_write and len(v_stored) and len(v_stored[self.store_variable.id][0]):
                logger.debug("Value already exist in RecordedData for %s - %s" % (d1, d1 + td))
                pass
            else:
                calc_value = self.get_value(d1, d1 + td)
                if calc_value is not None and self.store_variable.update_value(calc_value, td1):
                    item = self.store_variable.create_recorded_data_element()
                    item.date_saved = d1
                    if item is not None:
                        output.append(item)
            d1 = d1 + td

        if len(output):
            self.last_check = output[-1].date_saved  # + td
        else:
            #logger.debug("Nothing to add")
            self.last_check = min(d1, d2, now())

        # Add partial last value when then is data but the period is not elapsed
        # do not use this data in last check to recalculate it again till the period is elapsed
        calc_value = self.get_value(d2 - td, d2)
        td2 = (d2 - td).timestamp()
        if add_partial_info and calc_value is not None and self.store_variable.update_value(calc_value, td2):
            item = self.store_variable.create_recorded_data_element()
            item.date_saved = d2-td
            if item is not None:
                output.append(item)

        # Save recorded data elements to DB
        if len(output):
            m = "Adding : "
            for c in output:
                m += str(c) + " " + str(c.date_saved) + " - "
            logger.debug(m)
            RecordedData.objects.bulk_create(output, batch_size=100, ignore_conflicts=True)

        self.state = "Checked [%s to %s]" % (d1, d2)
        self.state = self.state[0:100]
        self.save(update_fields=['last_check', 'state'])

    def get_value(self, d1, d2):
        try:
            tmp = RecordedData.objects.get_values_in_time_range(variable=self.variable_calculated_fields.main_variable,
                                                                time_min=d1.timestamp(), time_max=d2.timestamp(),
                                                                time_in_ms=True,)
        except AttributeError:
            tmp = []
        values = []
        if len(tmp) > 0:
            for v in tmp[self.variable_calculated_fields.main_variable.id]:
                values.append(v[1])
            type_str = self.period.type_choices[self.period.type][1]
            if type_str == 'min':
                p = str(self.period.property)
                if p == '' or p is None or p == 'None':
                    res = min(values)
                elif p.startswith('<'):
                    try:
                        p = float(p.split('<')[1])
                        res = min_pass(values, p, 'gt')
                    except ValueError:
                        logger.warning("Period field %s property after < is not a float : %s" % (self.period, self.period.property))
                        res = None
                else:
                    try:
                        p = float(p)
                        res = min_pass(values, p, 'gte')
                    except ValueError:
                        logger.warning("Period field %s property is not a float : %s" % (self.period, self.period.property))
                        res = None
            elif type_str == 'max':
                p = str(self.period.property)
                if p == '' or p is None or p == 'None':
                    res = max(values)
                elif p.startswith('>'):
                    try:
                        p = float(p.split('>')[1])
                        res = max_pass(values, p, 'lt')
                    except ValueError:
                        logger.warning("Period field %s property after > is not a float : %s" % (self.period, self.period.property))
                        res = None
                else:
                    try:
                        p = float(p)
                        res = max_pass(values, p, 'lte')
                    except ValueError:
                        logger.warning("Period field %s property is not a float : %s" % (self.period, self.period.property))
                        res = None
            elif type_str == 'total':
                res = sum(values)
            elif type_str == 'difference':
                res = values[-1] - values[0]
            elif type_str == 'difference percent':
                res = (values[-1] - values[0]) / min(values)
            elif type_str == 'delta':
                res = 0
                v = None
                for i in values:
                    if v is not None and i - v > 0:
                        res += i - v
                    v = i
            elif type_str == 'mean':
                res = np.mean(values)
            elif type_str == 'first':
                res = values[0]
            elif type_str == 'last':
                res = values[-1]
            elif type_str == 'count':
                res = len(values)
            elif type_str == 'count value':
                try:
                    p = float(self.period.property)
                    res = values.count(p)
                except ValueError:
                    logger.warning("Period field %s property is not a float" % self.period)
                    res = None
            elif type_str == 'range':
                res = max(values) - min(values)
            elif type_str == 'step':
                res = 0
                j = None
                if len(values) > 1:
                    for i in values:
                        if j is not None:
                            res = min(res, abs(i - j))
                        j = i
                else:
                    res = None
            elif type_str == 'change count':
                res = 0
                j = None
                if len(values) > 1:
                    for i in values:
                        if j is not None and j != i:
                            res += 1
                        j = i
                else:
                    res = None
            elif type_str == 'distinct count':
                res = len(set(values))
            else:
                logger.warning ("Periodic field type unknown")
                res = None

            #logger.debug(str(d1) + " " + str(self.period) + " " + str(res))
            return res
        else:
            #logger.debug("No values for this period")
            return None

    def get_valid_range(self, d1, d2):
        if is_naive(d1):
            d1 = make_aware(d1)
        if is_naive(d2):
            d2 = make_aware(d2)
        if d2 <= d1:
            logger.warning("Use get_valid_range with d_start > d_end")
            return None
        if self.period.start_from == d1:
            d_start = 0
        else:
            d_start = self.period_diff_quantity(self.period.start_from, d1)
            if d_start is not None:
                if d_start != int(d_start):
                    d_start = int(d_start) + 1
                else:
                    d_start = int(d_start)
            else:
                logger.debug("d_start - start_from < period_factor*period")
                return None
        d_end = self.period_diff_quantity(self.period.start_from, d2)
        if d_end is not None:
            d_end = int(d_end)
        else:
            logger.debug("d_end - start_from < period_factor*period")
            return None
        if d_end <= d_start:
            logger.debug("d_end - d_start < period_factor*period")
            return None

        td = self.add_timedelta()

        d_start = d_start / self.period.period_factor
        if d_start != int(d_start):
            d_start = int(d_start) + 1
        else:
            d_start = int(d_start)

        d_end = d_end / self.period.period_factor
        if d_end != int(d_end):
            d_end = int(d_end) + 1
        else:
            d_end = int(d_end)

        dd_start = d_start * td + self.period.start_from
        dd_end = d_end * td + self.period.start_from

        if dd_end > d2:
            logger.debug("%s > %s" %(dd_end, d2))
            dd_end -= self.add_timedelta(self._period_diff_quantity(d2, dd_end))
            logger.debug("dd_end : %s" % dd_end)

        return [dd_start, dd_end]

    def add_timedelta(self, delta=None):
        if delta is None:
            delta = self.period.period_factor
        td = None
        period_str = self.period.period_choices[self.period.period][1]
        if period_str == 'year':
            td = monthdelta(12) * delta
        elif period_str == 'month':
            td = monthdelta(delta)
        elif period_str == 'week':
            td = datetime.timedelta(weeks=delta)
        elif period_str == 'day':
            td = datetime.timedelta(days=delta)
        elif period_str == 'hour':
            td = datetime.timedelta(hours=delta)
        elif period_str == 'minute':
            td = datetime.timedelta(minutes=delta)
        elif period_str == 'second':
            td = datetime.timedelta(seconds=delta)
        return td

    def _period_diff_quantity(self, d1, d2):
        period_str = self.period.period_choices[self.period.period][1]
        if period_str == 'year':
            res = self.years_diff_quantity(d1, d2)
        elif period_str == 'month':
            res = self.months_diff_quantity(d1, d2)
        elif period_str == 'week':
            res = self.weeks_diff_quantity(d1, d2)
        elif period_str == 'day':
            res = self.days_diff_quantity(d1, d2)
        elif period_str == 'hour':
            res = self.hours_diff_quantity(d1, d2)
        elif period_str == 'minute':
            res = self.minutes_diff_quantity(d1, d2)
        elif period_str == 'second':
            res = self.seconds_diff_quantity(d1, d2)
        return res

    def period_diff_quantity(self, d1, d2):
        res = self._period_diff_quantity(d1, d2)
        if res >= self.period.period_factor:
            return res
        else:
            return None

    def years_diff_quantity(self, d1, d2):
        return relativedelta.relativedelta(d2, d1).years

    def months_diff_quantity(self, d1, d2):
        return relativedelta.relativedelta(d2, d1).months + self.years_diff_quantity(d1, d2) * 12

    def weeks_diff_quantity(self, d1, d2):
        return self.days_diff_quantity(d1, d2) / 7

    def days_diff_quantity(self, d1, d2):
        diff = (d2 - d1).total_seconds() / 60 / 60 / 24
        #logger.debug("Days: " + str(diff))
        return diff

    def hours_diff_quantity(self, d1, d2):
        diff = (d2 - d1).total_seconds() / 60 / 60
        #logger.debug("Hours: " + str(diff))
        return diff

    def minutes_diff_quantity(self, d1, d2):
        diff = (d2 - d1).total_seconds() / 60
        #logger.debug("Minutes: " + str(diff))
        return diff

    def seconds_diff_quantity(self, d1, d2):
        diff = (d2 - d1).total_seconds()
        #logger.debug("Seconds: " + str(diff))
        return diff


class DeviceWriteTask(models.Model):
    id = models.AutoField(primary_key=True)
    variable = models.ForeignKey('Variable', blank=True, null=True, on_delete=models.SET_NULL)
    variable_property = models.ForeignKey('VariableProperty', blank=True, null=True, on_delete=models.SET_NULL)
    value = models.FloatField()
    user = models.ForeignKey(User, null=True, on_delete=models.SET_NULL)
    start = models.FloatField(default=0)  # TODO DateTimeField
    finished = models.FloatField(default=0, blank=True)  # TODO DateTimeField
    done = models.BooleanField(default=False, blank=True)
    failed = models.BooleanField(default=False, blank=True)

    def __str__(self):
        if self.variable:
            return self.variable.name
        elif self.variable_property:
            return self.variable_property.variable.name + ' : ' + self.variable_property.name
        else:
            return str(self.id)

    @property
    def get_device_id(self):
        if self.variable:
            return self.variable.device.pk
        elif self.variable_property:
            return self.variable_property.variable.device.pk
        else:
            return 0

    def create_and_notificate(self, dwts):
        if type(dwts) != list:
            dwts = [dwts]
        DeviceWriteTask.objects.bulk_create(dwts)
        if channels_driver:
            scheduler = BackgroundProcess.objects.filter(id=1)
            if len(scheduler):
                scheduler_pid = scheduler.first().pid
            else:
                logger.warning("No PID found for the scheduler")
                scheduler_pid = None
            for dwt in dwts:
                try:
                    device_id = dwt.get_device_id
                    for bp in BackgroundProcess.objects.all():
                        _device_id = bp.get_device_id()
                        if type(_device_id) == list and len(_device_id) > 0 and dwt.get_device_id in _device_id:
                            device_id = _device_id[0]
                            logger.debug(device_id)
                    channel_layer = channels.layers.get_channel_layer()
                    channel_layer.capacity = 1
                    async_to_sync(channel_layer.send)(str(scheduler_pid) + '_DeviceAction_for_' + str(device_id),
                                                      {'DeviceWriteTask': str(dwt.get_device_id)})
                except ChannelFull:
                    logger.info("Channel full : " + str(scheduler_pid) + '_DeviceAction_for_' + str(dwt.get_device_id))
                    pass
                except (AttributeError, ConnectionRefusedError, InvalidChannelLayerError):
                    pass


class DeviceReadTask(models.Model):
    id = models.AutoField(primary_key=True)
    device = models.ForeignKey('Device', blank=True, null=True, on_delete=models.SET_NULL)
    variable = models.ForeignKey('Variable', blank=True, null=True, on_delete=models.SET_NULL)
    variable_property = models.ForeignKey('VariableProperty', blank=True, null=True, on_delete=models.SET_NULL)
    user = models.ForeignKey(User, null=True, on_delete=models.SET_NULL)
    start = models.FloatField(default=0)  # TODO DateTimeField
    finished = models.FloatField(default=0, blank=True)  # TODO DateTimeField
    done = models.BooleanField(default=False, blank=True)
    failed = models.BooleanField(default=False, blank=True)

    def __str__(self):
        if self.variable:
            return self.variable.name
        elif self.variable_property:
            return self.variable_property.variable.name + ' : ' + self.variable_property.name
        elif self.device:
            return self.device.short_name
        else:
            return self.id

    @property
    def get_device_id(self):
        if self.device:
            return self.device.pk
        elif self.variable:
            return self.variable.device.pk
        elif self.variable_property:
            return self.variable_property.variable.device.pk
        else:
            return 0

    def create_and_notificate(self, drts):
        if type(drts) != list:
            drts = [drts]
        DeviceReadTask.objects.bulk_create(drts)
        if channels_driver:
            scheduler = BackgroundProcess.objects.filter(id=1)
            if len(scheduler):
                scheduler_pid = scheduler.first().pid
            else:
                logger.warning("No PID found for the scheduler")
                scheduler_pid = None
            for drt in drts:
                try:
                    device_id = drt.get_device_id
                    for bp in BackgroundProcess.objects.all():
                        _device_id = bp.get_device_id()
                        if type(_device_id) == list and len(_device_id) > 0 and drt.get_device_id in _device_id:
                            device_id = _device_id[0]
                    channel_layer = channels.layers.get_channel_layer()
                    channel_layer.capacity = 1
                    async_to_sync(channel_layer.send)(str(scheduler_pid) + '_DeviceAction_for_' + str(device_id),
                                                      {'DeviceReadTask': str(drt.get_device_id)})
                except ChannelFull:
                    logger.info("Channel full : " + str(scheduler_pid) + '_DeviceAction_for_' + str(drt.get_device_id))
                    pass
                except (AttributeError, ConnectionRefusedError, InvalidChannelLayerError):
                    pass


class RecordedDataOld(models.Model):
    """
    Big Int first 42 bits are used for the unixtime in ms, unsigned because we only
    store time values that are later than 1970, rest 21 bits are used for the
    variable id to have a uniqe primary key
    63 bit 111111111111111111111111111111111111111111111111111111111111111
    42 bit 111111111111111111111111111111111111111111000000000000000000000
    21 bit 										    1000000000000000000000
    """

    id = models.BigIntegerField(primary_key=True)
    value_boolean = models.BooleanField(default=False, blank=True)  # boolean
    value_int16 = models.SmallIntegerField(null=True, blank=True)  # int16, uint8, int8
    value_int32 = models.IntegerField(null=True, blank=True)  # uint8, int16, uint16, int32
    value_int64 = models.BigIntegerField(null=True, blank=True)  # uint32, int64
    value_float64 = models.FloatField(null=True, blank=True)  # float64
    variable = models.ForeignKey('Variable', null=True, on_delete=models.SET_NULL)
    objects = RecordedDataValueManager()

    def __init__(self, *args, **kwargs):
        if 'timestamp' in kwargs:
            timestamp = kwargs.pop('timestamp')
        else:
            timestamp = time.time()
        if 'variable_id' in kwargs:
            variable_id = kwargs['variable_id']
        elif 'variable' in kwargs:
            variable_id = kwargs['variable'].pk
        else:
            variable_id = None

        if variable_id is not None and 'id' not in kwargs:
            kwargs['id'] = int(int(int(timestamp * 1000) * 2097152) + variable_id)
        if 'variable' in kwargs and 'value' in kwargs:
            if kwargs['variable'].value_class.upper() in ['FLOAT', 'FLOAT64', 'DOUBLE', 'FLOAT32', 'SINGLE', 'REAL']:
                kwargs['value_float64'] = float(kwargs.pop('value'))
            elif kwargs['variable'].scaling and not kwargs['variable'].value_class.upper() in ['BOOL', 'BOOLEAN']:
                kwargs['value_float64'] = float(kwargs.pop('value'))
            elif kwargs['variable'].value_class.upper() in ['INT64', 'UINT32', 'DWORD']:
                kwargs['value_int64'] = int(kwargs.pop('value'))
                if kwargs['value_int64'].bit_length() > 64:
                    # todo throw exeption or do anything
                    pass
            elif kwargs['variable'].value_class.upper() in ['WORD', 'UINT', 'UINT16', 'INT32']:
                kwargs['value_int32'] = int(kwargs.pop('value'))
                if kwargs['value_int32'].bit_length() > 32:
                    # todo throw exeption or do anything
                    pass
            elif kwargs['variable'].value_class.upper() in ['INT16', 'INT8', 'UINT8', 'INT']:
                kwargs['value_int16'] = int(kwargs.pop('value'))
                if kwargs['value_int16'].bit_length() > 15:
                    # todo throw exeption or do anything
                    pass

            elif kwargs['variable'].value_class.upper() in ['BOOL', 'BOOLEAN']:
                kwargs['value_boolean'] = bool(kwargs.pop('value'))

        # call the django model __init__
        super(RecordedDataOld, self).__init__(*args, **kwargs)
        self.timestamp = self.time_value()

    def calculate_pk(self, timestamp=None):
        """
        calculate the primary key from the timestamp in seconds
        """
        if timestamp is None:
            timestamp = time.time()
        self.pk = int(int(int(timestamp * 1000) * 2097152) + self.variable.pk)

    def __str__(self):
        return str(self.value())

    def time_value(self):
        """
        return the timestamp in seconds calculated from the id
        """
        return (self.pk - self.variable.pk) / 2097152 / 1000.0  # value in seconds

    def value(self, value_class=None):
        """
        return the stored value
        """
        if value_class is None:
            value_class = self.variable.value_class

        if value_class.upper() in ['FLOAT', 'FLOAT64', 'DOUBLE', 'FLOAT32', 'SINGLE', 'REAL']:
            return self.value_float64
        elif self.variable.scaling and not value_class.upper() in ['BOOL', 'BOOLEAN']:
            return self.value_float64
        elif value_class.upper() in ['INT64', 'UINT32', 'DWORD']:
            return self.value_int64
        elif value_class.upper() in ['WORD', 'UINT', 'UINT16', 'INT32']:
            return self.value_int32
        elif value_class.upper() in ['INT16', 'INT8', 'UINT8']:
            return self.value_int16
        elif value_class.upper() in ['BOOL', 'BOOLEAN']:
            return self.value_boolean
        else:
            return None


class RecordedData(models.Model):
    """
    id: Big Int first 42 bits are used for the unix time in ms, unsigned because we only
    store values that are past 1970, the last 21 bits are used for the
    variable id to have a unique primary key
    63 bit 111111111111111111111111111111111111111111111111111111111111111
    42 bit 111111111111111111111111111111111111111111000000000000000000000
    21 bit 										    1000000000000000000000
    date_saved: datetime when the model instance is saved in the database (will be set in the save method)


    """

    id = models.BigIntegerField(primary_key=True)
    date_saved = models.DateTimeField(blank=True, null=True, db_index=True)
    value_boolean = models.BooleanField(default=False, blank=True)  # boolean
    value_int16 = models.SmallIntegerField(null=True, blank=True)  # int16, uint8, int8
    value_int32 = models.IntegerField(null=True, blank=True)  # uint8, int16, uint16, int32
    value_int64 = models.BigIntegerField(null=True, blank=True)  # uint32, int64, int48
    value_float64 = models.FloatField(null=True, blank=True)  # float64, float48
    variable = models.ForeignKey('Variable', null=True, on_delete=models.SET_NULL)
    objects = RecordedDataValueManager()

    #

    def __init__(self, *args, **kwargs):
        if 'timestamp' in kwargs:
            timestamp = kwargs.pop('timestamp')
        else:
            timestamp = time.time()

        if 'variable_id' in kwargs:
            variable_id = kwargs['variable_id']
        elif 'variable' in kwargs:
            variable_id = kwargs['variable'].pk
        else:
            variable_id = None

        if variable_id is not None and 'id' not in kwargs:
            kwargs['id'] = int(int(int(timestamp * 1000) * 2097152) + variable_id)
        if 'variable' in kwargs and 'value' in kwargs:
            if kwargs['variable'].value_class.upper() in ['FLOAT', 'FLOAT64', 'DOUBLE', 'FLOAT32', 'SINGLE', 'REAL',
                                                          'FLOAT48']:
                kwargs['value_float64'] = float(kwargs.pop('value'))
            elif kwargs['variable'].scaling and not kwargs['variable'].value_class.upper() in ['BOOL', 'BOOLEAN']:
                kwargs['value_float64'] = float(kwargs.pop('value'))
            elif kwargs['variable'].value_class.upper() in ['INT64', 'UINT32', 'DWORD', 'INT48']:
                kwargs['value_int64'] = int(kwargs.pop('value'))
                if kwargs['value_int64'].bit_length() > 64:
                    # todo throw exeption or do anything
                    pass
            elif kwargs['variable'].value_class.upper() in ['WORD', 'UINT', 'UINT16', 'INT32']:
                kwargs['value_int32'] = int(kwargs.pop('value'))
                if kwargs['value_int32'].bit_length() > 32:
                    # todo throw exeption or do anything
                    pass
            elif kwargs['variable'].value_class.upper() in ['INT16', 'INT8', 'UINT8', 'INT']:
                kwargs['value_int16'] = int(kwargs.pop('value'))
                if kwargs['value_int16'].bit_length() > 15:
                    # todo throw exeption or do anything
                    pass

            elif kwargs['variable'].value_class.upper() in ['BOOL', 'BOOLEAN']:
                kwargs['value_boolean'] = bool(kwargs.pop('value'))

        # call the django model __init__
        super(RecordedData, self).__init__(*args, **kwargs)
        if self.variable is not None:
            self.timestamp = self.time_value()
        else:
            self.timestamp = self.date_saved.timestamp()

    def calculate_pk(self, timestamp=None):
        """
        calculate the primary key from the timestamp in seconds
        """
        if timestamp is None:
            timestamp = time.time()
        self.pk = int(int(int(timestamp * 1000) * 2097152) + self.variable.pk)

    def __str__(self):
        return str(self.value())

    def time_value(self):
        """
        return the timestamp in seconds calculated from the id
        """
        return (self.pk - self.variable.pk) / 2097152 / 1000.0  # value in seconds

    def value(self, value_class=None):
        """
        return the stored value
        """
        if self.variable is None:
            return None

        if value_class is None:
            value_class = self.variable.value_class

        if value_class.upper() in ['FLOAT', 'FLOAT64', 'DOUBLE', 'FLOAT32', 'SINGLE', 'REAL', 'FLOAT48']:
            return self.value_float64
        elif self.variable.scaling and not value_class.upper() in ['BOOL', 'BOOLEAN']:
            return self.value_float64
        elif value_class.upper() in ['INT64', 'UINT32', 'DWORD', 'INT48']:
            return self.value_int64
        elif value_class.upper() in ['WORD', 'UINT', 'UINT16', 'INT32']:
            return self.value_int32
        elif value_class.upper() in ['INT16', 'INT8', 'UINT8']:
            return self.value_int16
        elif value_class.upper() in ['BOOL', 'BOOLEAN']:
            return self.value_boolean
        else:
            return None

    def save(self, *args, **kwargs):
        if self.date is None:
            self.date = now()
        super(RecordedData, self).save(*args, **kwargs)


class Log(models.Model):
    # id 				= models.AutoField(primary_key=True)
    id = models.BigIntegerField(primary_key=True)
    level = models.IntegerField(default=0, verbose_name="level")
    timestamp = models.FloatField()  # TODO DateTimeField
    message_short = models.CharField(max_length=400, default='', verbose_name="short message")
    message = models.TextField(default='', verbose_name="message")
    user = models.ForeignKey(User, null=True, on_delete=models.SET_NULL)

    def __init__(self, *args, **kwargs):
        if 'timestamp' in kwargs:
            timestamp = kwargs['timestamp']
        else:
            timestamp = time.time()
            kwargs['timestamp'] = timestamp
        if 'id' not in kwargs:
            if 'level' in kwargs:
                kwargs['id'] = int(int(int(timestamp * 1000) * 2097152) + kwargs['level'])
            else:
                kwargs['id'] = int(int(int(timestamp * 1000) * 2097152) + 0)
        super(Log, self).__init__(*args, **kwargs)

    def __str__(self):
        return self.message


class BackgroundProcess(models.Model):
    id = models.AutoField(primary_key=True)
    pid = models.IntegerField(default=0)
    label = models.CharField(max_length=400, default='')
    message = models.CharField(max_length=400, default='')
    enabled = models.BooleanField(default=False, blank=True)
    done = models.BooleanField(default=False, blank=True)
    failed = models.BooleanField(default=False, blank=True)
    parent_process = models.ForeignKey('BackgroundProcess', null=True, on_delete=models.SET_NULL, blank=True)
    process_class = models.CharField(max_length=400, blank=True, default='pyscada.utils.scheduler.Process',
                                     help_text="from pyscada.utils.scheduler import Process")
    process_class_kwargs = models.CharField(max_length=400, default='{}', blank=True,
                                            help_text='''arguments in json format will be passed as kwargs while the
                                            init of the process instance, example:
                                            {"keywordA":"value1", "keywordB":7}''')
    last_update = models.DateTimeField(null=True, blank=True)
    running_since = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name_plural = "Background Processes"

    def __str__(self):
        return self.label + ': ' + self.message

    def get_device_id(self):
        try:
            kwargs = json.loads(self.process_class_kwargs)
        except:
            kwargs = {}
        if 'device_id' in kwargs:
            return kwargs['device_id']
        elif 'device_ids' in kwargs:
            return kwargs['device_ids']
        else:
            return None

    def get_process_instance(self):
        # kwargs = dict(s.split("=") for s in self.process_class_kwargs.split())
        try:
            kwargs = json.loads(self.process_class_kwargs)
        except:
            kwargs = {}
        #
        kwargs['label'] = self.label
        kwargs['process_id'] = self.pk
        kwargs['parent_process_id'] = self.parent_process.pk

        class_name = self.process_class.split('.')[-1]
        class_path = self.process_class.replace('.' + class_name, '')
        try:
            mod = __import__(class_path, fromlist=[class_name.__str__()])
            process_class = getattr(mod, class_name.__str__())
            return process_class(**kwargs)
        except:
            logger.error('%s(%d), unhandled exception\n%s' % (self.label, getpid(), traceback.format_exc()))
            return None

    def restart(self):
        """
        restarts the process and all its child's

        :return:
        """
        if self.pid != 0 and self.pid is not None:

            try:
                kill(self.pid, signal.SIGUSR1)
                logger.debug('%d: send SIGUSR1 to %d' % (self.pk, self.pid))
                return True
            except OSError as e:
                return False

    def _stop(self, signum=signal.SIGTERM):
        """
        stops the process and all its child's

        :return:
        """
        if self.pid != 0 and self.pid is not None:
            logger.debug('send %s to daemon' % signum)
            try:
                kill(self.pid, signum)
            except OSError as e:
                if e.errno == errno.ESRCH:
                    try:
                        logger.debug('%s: process id %d is terminated' % (self, self.pid))
                        return True
                    except:
                        return False
            try:
                while True:
                    wpid, status = waitpid(self.pid, WNOHANG)
                    if not wpid:
                        break
            except:
                pass
            return False

    def stop(self, signum=signal.SIGTERM, cleanup=False):
        if cleanup:
            self.done = True
            self.save()
            timeout = time.time() + 30  # 30s timeout
            while time.time() < timeout:
                if self._stop(signum=signum):
                    return True
                time.sleep(1)
            if self._stop(signum=signal.SIGKILL):
                self.delete()
        else:
            return self._stop(signum=signum)


class ComplexEventGroup(models.Model):
    id = models.AutoField(primary_key=True)
    label = models.CharField(max_length=400, default='')
    complex_mail_recipients = models.ManyToManyField(User, blank=True)
    variable_to_change = models.ForeignKey(Variable, blank=True, null=True, default=None, on_delete=models.SET_NULL,
                                           related_name="complex_variable_to_change",
                                           help_text="Change the value on event changes")
    default_value = models.FloatField(default=None, blank=True, null=True, help_text="Set if no activated event")
    default_send_mail = models.BooleanField(default=False, help_text="Send mail if no activated event")
    last_level = models.SmallIntegerField(default=-1)

    def __str__(self):
        return self.label

    def do_event_check(self):
        """

        """
        item_found = None
        timestamp = time.time()
        active = False
        var_list_final = {}
        vp_list_final = {}

        for item in self.complexevent_set.all().order_by('order'):
            (is_valid, var_list, vp_list) = item.is_valid()
            if item_found is None and not active and self.last_level != item.level and is_valid:
                # logger.debug("item %s is valid : level %s" % (item, item.level))
                item_found = item
                var_list_final = var_list
                vp_list_final = vp_list
                self.last_level = item.level
                self.save()
                prev_event = RecordedEvent.objects.filter(complex_event_group=self, active=True)
                if prev_event:
                    if item.stop_recording:  # Stop recording
                        # logger.debug("stop recording")
                        prev_event = prev_event.last()
                        prev_event.active = False
                        prev_event.time_end = timestamp
                        prev_event.save()
                else:
                    if not item.stop_recording:  # Start Recording
                        # logger.debug("start recording")
                        prev_event = RecordedEvent(complex_event_group=self, time_begin=timestamp, active=True)
                        prev_event.save()
            active = active or item.active

        if item_found is not None:
            if item_found.send_mail:  # Send Mail
                (subject, message, html_message,) = self.compose_mail(item_found, var_list_final, vp_list_final)
                for recipient in self.complex_mail_recipients.exclude(email=''):
                    Mail(None, subject, message, html_message, recipient.email, time.time()).save()

            # Change value
            if item_found.new_value is not None and self.variable_to_change is not None:
                # and self.variable_to_change.update_value(item_found.new_value, timestamp):
                user, _ = User.objects.get_or_create(username='ComplexEvents')
                dwt = DeviceWriteTask(variable=self.variable_to_change, value=item_found.new_value, user=user,
                                      start=timestamp)
                dwt.create_and_notificate(dwt)
                #temp_item = self.variable_to_change.create_recorded_data_element()
                #temp_item.date_saved = now()
                #RecordedData.objects.bulk_create([temp_item])

        elif not active and self.last_level != -1:
            self.last_level = -1
            self.save()
            if self.default_value is not None:
                #and self.variable_to_change.update_value(self.default_value, timestamp):
                user, _ = User.objects.get_or_create(username='ComplexEvents')
                dwt = DeviceWriteTask(variable=self.variable_to_change, value=self.default_value, user=user,
                                      start=timestamp)
                dwt.create_and_notificate(dwt)
                #temp_item = self.variable_to_change.create_recorded_data_element()
                #temp_item.date_saved = now()
                #RecordedData.objects.bulk_create([temp_item])
            if self.default_send_mail:
                (subject, message, html_message,) = self.compose_mail(None, {}, {})
                for recipient in self.complex_mail_recipients.exclude(email=''):
                    Mail(None, subject, message, html_message, recipient.email, time.time()).save()

            # logger.debug("level = -1")
            # No active event : stop recording
            prev_event = RecordedEvent.objects.filter(complex_event_group=self, active=True)
            if prev_event:
                # logger.debug("stop recording2")
                prev_event = prev_event.last()
                prev_event.active = False
                prev_event.time_end = timestamp
                prev_event.save()

    def compose_mail(self, item_found, var_list, vp_list):
        if hasattr(settings, 'EMAIL_PREFIX'):
            subject_str = settings.EMAIL_PREFIX
        else:
            subject_str = ''

        if item_found is not None and item_found.active:
            if item_found.level == 0:  # infomation
                subject_str += " - Information - "
            elif item_found.level == 1:  # Ok
                subject_str += " - Ok - "
            elif item_found.level == 2:  # warning
                subject_str += " - Warning! - "
            elif item_found.level == 3:  # alert
                subject_str += " - Alert! - "
            subject_str += self.label + " - An event is active"
            message_str = "The event group " + self.label + " has been triggered<br>"
            message_str += "Level : " + item_found.level_choices[item_found.level][1] + "<br>"
            message_str += "Validation : " + item_found.validation_choices[item_found.validation][1] + "<br>"
        else:
            subject_str += " - Information - "
            subject_str += self.label + " No active event"
            message_str = "The event group " + self.label + " has no active events<br>"

        message_str += "Date : " + str(datetime.datetime.now().isoformat()) + "<br><br>"

        for i in var_list:
            message_str += str(var_list[i]['type']) + " : " + str(var_list[i]['name']) + " (" + str(i) + ") : "
            in_limit_str = "<span style='color:red;'>" + str(var_list[i]['in_limit']) + "</span>" if \
                var_list[i]['in_limit'] else str(var_list[i]['in_limit'])
            message_str += in_limit_str + "<br>"
            message_str += "Last value on "
            message_str += str(datetime.datetime.isoformat(datetime.datetime.utcfromtimestamp(var_list[i]['datetime'])))
            message_str += " = "
            if var_list[i]['label'] is None:
                message_str += str(var_list[i]['value']) + "<br>"
            else:
                message_str += str(var_list[i]['label']) + " (" + str(var_list[i]['value']) + ")<br>"

            message_str += "Limit rules : "
            if var_list[i]['limit_low_type'] == 0:
                limit_low_type = "< "
            else:
                limit_low_type = "<= "
            if var_list[i]['limit_high_type'] == 0:
                limit_high_type = "< "
            else:
                limit_high_type = "<= "
            if (var_list[i]['hysteresis_low'] == 0 or var_list[i]['limit_low_value'] is None) and \
                    (var_list[i]['hysteresis_high'] == 0 or var_list[i]['limit_high_value'] is None):
                if var_list[i]['limit_low_value'] is not None:
                    message_str += str(var_list[i]['limit_low_value']) + str(limit_low_type)
                message_str += " value "
                if var_list[i]['limit_high_value'] is not None:
                    message_str += str(limit_high_type) + str(var_list[i]['limit_high_value'])
                message_str += "<br><br>"
            else:
                message_str += "To enter the limit : <br>"
                if var_list[i]['limit_low_value'] is not None:
                    message_str += str(var_list[i]['limit_low_value'] + var_list[i]['hysteresis_low'])
                    message_str += str(limit_low_type)
                message_str += " value "
                if var_list[i]['limit_high_value'] is not None:
                    message_str += str(limit_high_type)
                    message_str += str(var_list[i]['limit_high_value'] - var_list[i]['hysteresis_high'])
                message_str += "<br>"
                message_str += "To leave the limit : <br>"
                if var_list[i]['limit_low_value'] is not None:
                    message_str += str(var_list[i]['limit_low_value'] - var_list[i]['hysteresis_low'])
                    message_str += str(limit_low_type)
                message_str += " value "
                if var_list[i]['limit_high_value'] is not None:
                    message_str += str(limit_high_type)
                    message_str += str(var_list[i]['limit_high_value'] + var_list[i]['hysteresis_high'])
                message_str += "<br><br>"
        for i in vp_list:
            message_str += str(vp_list[i]['type']) + " : " + str(vp_list[i]['name']) + " (" + str(i) + ") : "
            in_limit_str = "<span style='color:red;'>" + str(vp_list[i]['in_limit']) + "</span>" if \
                vp_list[i]['in_limit'] else str(vp_list[i]['in_limit'])
            message_str += in_limit_str + "<br>"
            message_str += "Last value on "
            message_str += str(datetime.datetime.isoformat(vp_list[i]['datetime']))
            message_str += " = "
            if vp_list[i]['label'] is None:
                message_str += str(vp_list[i]['value']) + "<br>"
            else:
                message_str += str(vp_list[i]['label']) + " (" + str(vp_list[i]['value']) + ")<br>"
            message_str += "Limit rules : "
            if vp_list[i]['limit_low_type'] == 0:
                limit_low_type = "<"
            else:
                limit_low_type = "<="
            if vp_list[i]['limit_high_type'] == 0:
                limit_high_type = "<"
            else:
                limit_high_type = "<="
            if vp_list[i]['hysteresis_low'] == 0 and vp_list[i]['hysteresis_high'] == 0:
                if vp_list[i]['limit_low_value'] is not None:
                    message_str += str(vp_list[i]['limit_low_value']) + str(limit_low_type)
                message_str += " value "
                if vp_list[i]['limit_high_value'] is not None:
                    message_str += str(limit_high_type) + str(vp_list[i]['limit_high_value'])
                message_str += "<br>"
            else:
                message_str += "To enter the limit : <br>"
                if vp_list[i]['limit_low_value'] is not None:
                    message_str += str(vp_list[i]['limit_low_value'] + vp_list[i]['hysteresis_low'])
                    message_str += str(limit_low_type)
                message_str += " value "
                if vp_list[i]['limit_high_value'] is not None:
                    message_str += str(limit_high_type)
                    message_str += str(vp_list[i]['limit_high_value'] - vp_list[i]['hysteresis_high'])
                message_str += "<br>"
                message_str += "To leave the limit : <br>"
                if vp_list[i]['limit_low_value'] is not None:
                    message_str += str(vp_list[i]['limit_low_value'] - vp_list[i]['hysteresis_low'])
                    message_str += str(limit_low_type)
                message_str += " value "
                if vp_list[i]['limit_high_value'] is not None:
                    message_str += str(limit_high_type)
                    message_str += str(vp_list[i]['limit_high_value'] + vp_list[i]['hysteresis_high'])
                message_str += "<br><br>"
        return subject_str, "", message_str


class ComplexEvent(models.Model):
    id = models.AutoField(primary_key=True)
    level_choices = (
        (0, 'informative'),
        (1, 'ok'),
        (2, 'warning'),
        (3, 'alert'),
    )
    level = models.PositiveSmallIntegerField(default=0, choices=level_choices)
    send_mail = models.BooleanField(default=False)
    new_value = models.FloatField(default=None, blank=True, null=True, help_text="For the group variable to change")
    order = models.PositiveSmallIntegerField(default=0)
    stop_recording = models.BooleanField(default=False)
    validation_choices = (
        (0, 'OR'),
        (1, 'AND'),
        (2, 'Custom'),
    )
    validation = models.PositiveSmallIntegerField(default=0, choices=validation_choices)
    custom_validation = models.CharField(max_length=400, default='', blank=True, null=True)
    active = models.BooleanField(default=False)
    complex_event_group = models.ForeignKey(ComplexEventGroup, on_delete=models.CASCADE)

    def is_valid(self):
        valid = False
        vars_infos = {}
        vp_infos = {}
        if self.validation == 0:  # OR
            valid = False
        elif self.validation == 1 and self.complexeventitem_set.count():  # AND
            valid = True
        for item in self.complexeventitem_set.all():
            (in_limit, item_info) = item.in_limit()
            if in_limit is None:
                if self.validation == 1:
                    valid = False
                continue
            if in_limit:
                if self.validation == 0:
                    valid = True
            else:
                if self.validation == 1:
                    valid = False
            if item.get_type() == 'variable' and len(item_info):
                vars_infos[item.get_id()] = item_info
            elif item.get_type() == 'variable_property' and len(item_info):
                vp_infos[item.get_id()] = item_info
        if self.active != valid:
            self.active = valid
            self.save()
        return valid, vars_infos, vp_infos

    def __str__(self):
        return self.complex_event_group.label + "-" + self.level_choices[self.level][1]


class ComplexEventItem(models.Model):
    id = models.AutoField(primary_key=True)
    fixed_limit_low = models.FloatField(default=0, blank=True, null=True)
    variable_limit_low = models.ForeignKey(Variable, blank=True, null=True, default=None, on_delete=models.SET_NULL,
                                           related_name="variable_limit_low", help_text='''you can choose either an
                                            fixed limit or an variable limit that is dependent on the current value of
                                            an variable, if you choose a value other than  none for variable limit the
                                            fixed limit would be ignored''')
    limit_low_type_choices = (
        (0, 'limit < value',),
        (1, 'limit <= value',),
    )
    limit_low_type = models.PositiveSmallIntegerField(default=0, choices=limit_low_type_choices)
    hysteresis_low = models.FloatField(default=0)
    variable = models.ForeignKey(Variable, related_name="variable", blank=True, null=True,
                                 on_delete=models.CASCADE)
    variable_property = models.ForeignKey(VariableProperty, blank=True, null=True, on_delete=models.CASCADE)
    fixed_limit_high = models.FloatField(default=0, blank=True, null=True)
    variable_limit_high = models.ForeignKey(Variable, blank=True, null=True, default=None, on_delete=models.SET_NULL,
                                            related_name="variable_limit_high", help_text='''you can choose either an
                                            fixed limit or an variable limit that is dependent on the current value of
                                            an variable, if you choose a value other than  none for variable limit the
                                            fixed limit would be ignored''')
    limit_high_type_choices = (
        (0, 'value < limit',),
        (1, 'value <= limit',),
    )
    limit_high_type = models.PositiveSmallIntegerField(default=0, choices=limit_high_type_choices)
    hysteresis_high = models.FloatField(default=0)
    active = models.BooleanField(default=False)
    complex_event = models.ForeignKey(ComplexEvent, on_delete=models.CASCADE)

    def in_limit(self):
        item_value = None
        item_date = None
        item_type = None
        item_name = None
        item_dict_label = None
        limit_low = None
        limit_high = None

        if self.variable is not None and self.variable.active:
            if self.variable.query_prev_value(time_min=0):
                item_value = self.variable.prev_value
                item_date = self.variable.timestamp_old
            item_type = 'variable'
            item_name = self.variable.name
        elif self.variable_property is not None:
            item_value = self.variable_property.value()
            item_date = self.variable_property.last_modified
            if type(item_value) != int and type(item_value) != float:
                item_value = None
            item_type = 'variable_property'
            item_name = self.variable_property.name

        var_info = {'value': item_value,
                    'datetime': item_date,
                    'type': item_type,
                    'name': item_name,
                    'limit_low_type': self.limit_low_type_choices[self.limit_low_type][0],
                    'limit_low_value': limit_low,
                    'hysteresis_low': self.hysteresis_low,
                    'limit_high_type': self.limit_high_type_choices[self.limit_high_type][0],
                    'limit_high_value': limit_high,
                    'hysteresis_high': self.hysteresis_high,
                    'label': item_dict_label,
                    'in_limit': None,
                    }

        if item_value is not None:
            if self.variable_limit_low is not None:
                if self.variable_limit_low.query_prev_value(time_min=0):
                    limit_low = self.variable_limit_low.prev_value
                else:
                    limit_low = None
            else:
                limit_low = self.fixed_limit_low
            if self.variable_limit_high is not None:
                if self.variable_limit_high.query_prev_value(time_min=0):
                    limit_high = self.variable_limit_high.prev_value
                else:
                    limit_high = None
            else:
                limit_high = self.fixed_limit_high
            if limit_low is None and limit_high is None:
                return None, var_info
            var_info['limit_low_value'] = limit_low
            var_info['limit_high_value'] = limit_high
            if self.variable is not None and self.variable.dictionary is not None:
                var_info['label'] = self.variable.dictionary.get_label(item_value)
            elif self.variable_property is not None and self.variable_property.dictionary is not None:
                var_info['label'] = self.variable_property.dictionary.get_label(item_value)

            actived = self.active
            if limit_low is not None and self.limit_low_type == 0 and item_value <= \
                    (limit_low + self.hysteresis_low * np.power(-1, self.active)):
                var_info['in_limit'] = False
                self.active = False
            elif limit_low is not None and self.limit_low_type == 1 and item_value < \
                    (limit_low + self.hysteresis_low * np.power(-1, self.active)):
                var_info['in_limit'] = False
                self.active = False
            elif limit_high is not None and self.limit_high_type == 0 and \
                    (limit_high - self.hysteresis_high * np.power(-1, self.active)) <= item_value:
                var_info['in_limit'] = False
                self.active = False
            elif limit_high is not None and self.limit_high_type == 1 and \
                    (limit_high - self.hysteresis_high * np.power(-1, self.active)) < item_value:
                var_info['in_limit'] = False
                self.active = False
            else:
                var_info['in_limit'] = True
                self.active = True
            if actived != self.active:
                self.save()
            return self.active, var_info
        return None, var_info

    def get_id(self):
        if self.variable is not None:
            return self.variable.pk
        elif self.variable_property is not None:
            return self.variable_property.pk

    def get_type(self):
        if self.variable is not None:
            return 'variable'
        elif self.variable_property is not None:
            return 'variable_property'


class Event(models.Model):
    id = models.AutoField(primary_key=True)
    label = models.CharField(max_length=400, default='')
    variable = models.ForeignKey(Variable, null=True, on_delete=models.CASCADE)
    level_choices = (
        (0, 'informative'),
        (1, 'ok'),
        (2, 'warning'),
        (3, 'alert'),
    )
    level = models.PositiveSmallIntegerField(default=0, choices=level_choices)
    fixed_limit = models.FloatField(default=0, blank=True, null=True)
    variable_limit = models.ForeignKey(Variable, blank=True, null=True, default=None, on_delete=models.SET_NULL,
                                       related_name="variable_limit",
                                       help_text='''you can choose either an fixed limit or an variable limit that is
                                        dependent on the current value of an variable, if you choose a value other than
                                        none for variable limit the fixed limit would be ignored''')
    limit_type_choices = (
        (0, 'value < limit',),
        (1, 'value <= limit',),
        (2, 'limit < value'),
        (3, 'limit <= value'),
        (4, 'value == limit'),
    )
    limit_type = models.PositiveSmallIntegerField(default=0, choices=limit_type_choices)
    hysteresis = models.FloatField(default=0)
    action_choices = (
        (0, 'just record'),
        (1, 'record and send mail only when event occurs'),
        (2, 'record and send mail'),
        (3, 'record, send mail and change variable'),
    )
    action = models.PositiveSmallIntegerField(default=0, choices=action_choices)
    mail_recipients = models.ManyToManyField(User)
    variable_to_change = models.ForeignKey(Variable, blank=True, null=True, default=None, on_delete=models.SET_NULL,
                                           related_name="variable_to_change")
    new_value = models.FloatField(default=0, blank=True, null=True)

    def __str__(self):
        return self.label

    def do_event_check(self):
        """
        compare the actual value with the limit value

        (0,'value is below the limit',),
        (1,'value is less than or equal to the limit',),
        (2,'value is greater than the limit'),
        (3,'value is greater than or equal to the limit'),
        (4,'value equals the limit'),
        """

        def compose_mail(active):
            if hasattr(settings, 'EMAIL_PREFIX'):
                subject_str = settings.EMAIL_PREFIX
            else:
                subject_str = ''

            if active:
                if self.level == 0:  # infomation
                    subject_str += " Information "
                elif self.level == 1:  # Ok
                    subject_str += " "
                elif self.level == 2:  # warning
                    subject_str += " Warning! "
                elif self.level == 3:  # alert
                    subject_str += " Alert! "
                subject_str += self.variable.name + " exceeded the limit"
            else:
                subject_str += " Information "
                subject_str += self.variable.name + " is back in limit"
            message_str = "The Event " + self.label + " has been triggered\n"
            message_str += "Value of " + self.variable.name + " is " + actual_value.__str__() + " " + self.variable.unit.unit
            message_str += " Limit is " + limit_value.__str__() + " " + self.variable.unit.unit
            return subject_str, message_str

        #
        # get recorded event
        prev_event = RecordedEvent.objects.filter(event=self, active=True)
        if prev_event:
            prev_value = True
        else:
            prev_value = False
        # get the actual value
        # actual_value = RecordedDataCache.objects.filter(variable=self.variable).last() # TODO change to RecordedData
        actual_value = RecordedData.objects.last_element(variable=self.variable)
        if not actual_value:
            return False
        timestamp = actual_value.time_value()
        actual_value = actual_value.value()
        # determine the limit type, variable or fixed
        if self.variable_limit:
            # item has a variable limit
            # get the limit value
            # limit_value = RecordedDataCache.objects.filter(variable=self.variable_limit) # TODO change to RecordedData
            limit_value = RecordedData.objects.last_element(variable=self.variable_limit)
            if not limit_value:
                return False
            if timestamp < limit_value.last().time_value():
                # when limit value has changed after the actual value take that time
                timestamp = limit_value.last().time_value()
            limit_value = limit_value.last().value()  # get value
        else:
            # item has a fixed limit
            limit_value = self.fixed_limit

        if self.limit_type == 0:
            if prev_value:
                limit_check = actual_value < (limit_value + self.hysteresis)
            else:
                limit_check = actual_value < (limit_value - self.hysteresis)
        elif self.limit_type == 1:
            if prev_value:
                limit_check = actual_value <= (limit_value + self.hysteresis)
            else:
                limit_check = actual_value <= (limit_value - self.hysteresis)
        elif self.limit_type == 4:
            limit_check = limit_value + self.hysteresis >= actual_value >= limit_value - self.hysteresis
        elif self.limit_type == 3:
            if prev_value:
                limit_check = actual_value >= (limit_value - self.hysteresis)
            else:
                limit_check = actual_value >= (limit_value + self.hysteresis)
        elif self.limit_type == 2:
            if prev_value:
                limit_check = actual_value > (limit_value - self.hysteresis)
            else:
                limit_check = actual_value > (limit_value + self.hysteresis)
        else:
            return False

        # record event
        if limit_check:  # value is outside of the limit
            if not prev_event:
                # if there is no previus event record the Event
                prev_event = RecordedEvent(event=self, time_begin=timestamp, active=True)
                prev_event.save()

                if self.action >= 1:
                    # compose and send mail
                    (subject, message,) = compose_mail(True)
                    for recipient in self.mail_recipients.exclude(email=''):
                        Mail(None, subject, message, None, recipient.email, time.time()).save()

                if self.action >= 3:
                    # do action
                    if self.variable_to_change:
                        DeviceWriteTask(variable=self.variable_to_change, value=self.new_value, start=timestamp)
        else:  # back inside of limit
            if prev_event:  #
                prev_event = prev_event.last()
                prev_event.active = False
                prev_event.time_end = timestamp
                prev_event.save()

                if self.action >= 2:
                    # compose and send mail
                    (subject, message,) = compose_mail(False)
                    for recipient in self.mail_recipients.exclude(email=''):
                        Mail(None, subject, message, None, recipient.email, time.time()).save()


class RecordedEvent(models.Model):
    id = models.AutoField(primary_key=True)
    event = models.ForeignKey(Event, null=True, on_delete=models.CASCADE)
    complex_event_group = models.ForeignKey(ComplexEventGroup, null=True, on_delete=models.CASCADE)
    time_begin = models.FloatField(default=0)  # TODO DateTimeField
    time_end = models.FloatField(null=True, blank=True)  # TODO DateTimeField
    active = models.BooleanField(default=False, blank=True)

    def __str__(self):
        if self.event:
            return self.event.label
        elif self.complex_event_group:
            return self.complex_event_group.label


class Mail(models.Model):
    id = models.AutoField(primary_key=True)
    subject = models.TextField(default='', blank=True)
    message = models.TextField(default='', blank=True)
    html_message = models.TextField(null=True, blank=True)
    to_email = models.EmailField(max_length=254)
    timestamp = models.FloatField(default=0)  # TODO DateTimeField
    done = models.BooleanField(default=False, blank=True)
    send_fail_count = models.PositiveSmallIntegerField(default=0)

    def send_mail(self):
        # TODO check email limit
        # blocked_recipient = [] # list of blocked mail recipoients
        # mail_count_limit = 200 # send max 200 Mails per 24h per user
        #
        # for recipient in mail.mail_recipients.exclude(to_email__in=blocked_recipient):
        # 	if recipient.mail_set.filter(timestamp__gt=time()-(60*60*24)).count() > self.mail_count_limit:
        # 		blocked_recipient.append(recipient.pk)
        if self.send_fail_count >= 3 or self.done:
            # only try to send an email three times
            return False
        # send the mail
        try:
            if send_mail(self.subject, self.message, settings.DEFAULT_FROM_EMAIL, [self.to_email], fail_silently=True,
                         html_message=self.html_message):
                self.done = True
                self.timestamp = time.time()
                self.save()
                return True
            else:
                self.send_fail_count = self.send_fail_count + 1
                self.timestamp = time.time()
                self.save()
                return False
        except (IndexError, ValueError) as e:
            logger.debug("Mail exception : %s" % e)
            self.send_fail_count = self.send_fail_count + 1
            self.timestamp = time.time()
            self.save()
            return False

    def __str__(self):
        return self.id.__str__()
