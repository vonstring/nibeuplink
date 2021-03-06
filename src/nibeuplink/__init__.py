import logging
from itertools import islice
import asyncio
import aiohttp
import uuid
from collections import namedtuple
from datetime import datetime, timedelta

from urllib.parse import urlencode, urlsplit, parse_qs


_LOGGER = logging.getLogger(__name__)

MAX_REQUEST_PARAMETERS = 15


ClimateSystem = namedtuple(
    'ClimateSystem',
    [
        'name',
        'return_temp',                 # BT3
        'supply_temp',                 # BT2
        'calc_supply_temp_heat',       # CSTH
        'offset_cool',                 # OC
        'room_temp',                   # BT50
        'room_setpoint_heat',          # RSH
        'room_setpoint_cool',          # RSC
        'use_room_sensor',             # URS
        'active_accessory',            # AA
        'external_adjustment_active',  # EAA
        'calc_supply_temp_cool',       # CSTC
        'offset_heat',                 # OH
        'heat_curve',                  # HC
        'min_supply',                  # MIS
        'max_supply',                  # MAS
        'extra_heat_pump'              # EHP
    ]
)

PARAM_CLIMATE_SYSTEMS = {
    #                        BT3    BT2    CSTH   OC     BT50   RSH    RSC    URS    AA     EAA    CSTC   OH     HC     MIS    MAS    HP
    '1': ClimateSystem('S1', 40012, 40008, 43009, 48739, 40033, 47398, 48785, 47394, None , 43161, 44270, 47011, 47007, 47015, 47016, None),
    '2': ClimateSystem('S2', 40129, 40007, 43008, 48738, 40032, 47397, 48784, 47393, 47302, 43160, 44269, 47010, 47006, 47014, 47017, 44746),
    '3': ClimateSystem('S3', 40128, 40006, 43007, 48737, 40031, 47396, 48783, 47392, 47303, 43159, 44268, 47009, 47005, 47013, 47018, 44745),
    '4': ClimateSystem('S4', 40127, 40005, 43006, 48736, 40030, 47395, 48782, 47391, 47304, 43158, 44267, 47008, 47004, 47012, 47019, 44744),
}

PARAM_PUMP_SPEED = 43437


def chunks(data, SIZE):
    it = iter(data)
    for i in range(0, len(data), SIZE):
        yield {k: data[k] for k in islice(it, SIZE)}


def chunk_pop(data, SIZE):
    count = len(data)
    if count > SIZE:
        count = SIZE

    res = data[0:count]
    del data[0:count]
    return res


async def raise_for_status(response):
    if 400 <= response.status:
        raise aiohttp.ClientResponseError(
            response.request_info,
            response.history,
            code=response.status,
            message=await response.text(),
            headers=response.headers)


class BearerAuth(aiohttp.BasicAuth):
    def __init__(self, access_token):
        self.access_token = access_token

    def encode(self):
        return "Bearer {}".format(self.access_token)


class ParameterRequest:
    def __init__(self, parameter_id: str):
        self.parameter_id = parameter_id
        self.data         = None
        self.done         = False


class Uplink():

    THROTTLE = timedelta(seconds = 4)

    def __init__(self,
                 client_id,
                 client_secret,
                 redirect_uri,
                 access_data,
                 access_data_write,
                 scope     = ['READSYSTEM'],
                 loop      = None,
                 base      = 'https://api.nibeuplink.com'):

        self.redirect_uri      = redirect_uri
        self.client_id         = client_id
        self.access_data_write = access_data_write
        self.state             = None
        self.scope             = scope
        self.lock              = asyncio.Lock()
        self.session           = None
        self.loop              = loop
        self.base              = base
        self.access_data       = None

        # check that the access scope is enough, otherwise ignore
        if access_data:
            if set(scope).issubset(set(access_data['scope'].split(' '))):
                self.access_data = access_data
            else:
                _LOGGER.info("Ignoring access data due to changed scope {}".format(scope))

        if self.access_data:
            self.auth = BearerAuth(self.access_data['access_token'])
        else:
            self.auth = None

        headers = {
            'Accept'      : 'application/json',
            'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8'
        }

        self.session           = aiohttp.ClientSession(headers   = headers,
                                                       auth      = aiohttp.BasicAuth(client_id, client_secret))
        self.requests          = {}
        self.timestamp         = datetime.now()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def close(self):
        if self.session:
            await self.session.close()
            self.session = None

    def _handle_access_token(self, data):
        if 'access_token' not in data:
            raise ValueError('Error in reply {}'.format(data))

        self.access_data = data
        if self.access_data_write:
            self.access_data_write(data)
        self.auth = BearerAuth(self.access_data['access_token'])

    async def get_access_token(self, code):
        payload = {
            'grant_type'   : 'authorization_code',
            'code'         : code,
            'redirect_uri' : self.redirect_uri,
        }

        async with self.session.post('{}/oauth/token'.format(self.base),
                                     data=payload) as response:
            await raise_for_status(response)
            self._handle_access_token(await response.json())

    async def refresh_access_token(self):
        payload = {
            'grant_type'    : 'refresh_token',
            'refresh_token' : self.access_data['refresh_token'],
        }

        async with self.session.post('{}/oauth/token'.format(self.base),
                                     data=payload) as response:
            await raise_for_status(response)
            self._handle_access_token(await response.json())

    def get_authorize_url(self):
        self.state = uuid.uuid4().hex

        params = {
            'response_type' : 'code',
            'client_id'     : self.client_id,
            'redirect_uri'  : self.redirect_uri,
            'scope'         : ' '.join(self.scope),
            'state'         : self.state,
        }

        return '{}/oauth/authorize?{}'.format(self.base, urlencode(params))

    def get_code_from_url(self, url):
        query = parse_qs(urlsplit(url).query)
        if 'state' in query and query['state'][0] != self.state:
            raise ValueError('Invalid state in url {} expected {}'.format(query['state'], self.state))
        return query['code'][0]

    async def _get_throttle(self):
        """ Throttle requests to API to once every MIN_REQUEST_DELAY """
        timestamp = datetime.now()

        delay = (self.timestamp - timestamp).total_seconds()
        if delay > 0:
            await asyncio.sleep(delay)
        self.timestamp = timestamp + self.THROTTLE

    async def get(self, url, params = {}):
        async with self.lock:
            await self._get_throttle()

            return await self._request(
                self.session.get,
                '{}/api/v1/{}'.format(self.base, url),
                params = params,
                headers= {},
            )

    async def _request(self, fun, *args, **kw):

        response = await fun(*args, auth = self.auth, **kw)
        try:
            if response.status == 401:
                _LOGGER.debug(response)
                _LOGGER.info("Attempting to refresh token due to error in request")
                await self.refresh_access_token()
                response.close()
                response = await fun(*args, auth=self.auth, **kw)

            await raise_for_status(response)

            if 'json' in response.headers.get('CONTENT-TYPE', ''):
                data = await response.json()
            else:
                data = await response.text()

            return data

        finally:
            response.close()

    async def get_parameter_raw(self, system_id: int, parameter_id: str):

        request = ParameterRequest(str(parameter_id))
        if system_id not in self.requests:
            self.requests[system_id] = []
        self.requests[system_id].append(request)

        # yield to any other runnable that want to add requests
        await asyncio.sleep(0)

        while True:
            async with self.lock:

                # check if we are already finished, by somebody elses request
                if request.done:
                    break

                if len(self.requests[system_id]) == 0:
                    break

                # Throttle requests to API, during this
                # new requests can be added
                await self._get_throttle()

                # chop of as many requests from start as possible
                requests = chunk_pop(self.requests[system_id],
                                     MAX_REQUEST_PARAMETERS)

                _LOGGER.debug("Requesting parameters {}".format([str(x.parameter_id) for x in requests]))

                data = await self._request(
                    self.session.get,
                    '{}/api/v1/systems/{}/parameters'.format(self.base, system_id),
                    params  = [('parameterIds', str(x.parameter_id)) for x in requests],
                    headers = {},
                )

                lookup = {p['name']: p for p in data}

                for r in requests:
                    r.done = True
                    if r.parameter_id in lookup:
                        r.data = lookup[r.parameter_id]

        return request.data

    def add_parameter_extensions(self, data):
        if data:
            if data['displayValue'].endswith(data['unit']) and len(data['unit']):
                value = data['displayValue'][:-len(data['unit'])]

                try:
                    value = float(value)
                except ValueError:
                    pass

                data['value'] = value
            elif data['displayValue'] == '--':
                data['value'] = None
            else:
                data['value'] = data['displayValue']

    async def get_parameter(self, system_id: int, parameter_id: str):
        data = await self.get_parameter_raw(system_id, parameter_id)
        self.add_parameter_extensions(data)
        return data

    async def put_parameter(self, system_id: int, parameter_id: str, value):
        headers = {
            'Accept'      : 'application/json',
            'Content-Type': 'application/json;charset=UTF-8'
        }

        data = {
            'settings': {
                str(parameter_id): str(value)
            }
        }

        data = await self._request(
            self.session.put,
            '{}/api/v1/systems/{}/parameters'.format(self.base, system_id),
            json    = data,
            headers = headers,
        )
        return data[0]['status']

    async def get_system(self, system_id: int):
        _LOGGER.debug("Requesting system {}".format(system_id))
        return await self.get('systems/{}'.format(system_id))

    async def get_systems(self):
        _LOGGER.debug("Requesting systems")
        data = await self.get('systems')
        return data['objects']

    async def get_category_raw(self,
                               system_id: int,
                               category_id: str,
                               unit_id: int = 0):
        _LOGGER.debug("Requesting category {} on system {}".format(category_id, system_id))
        return await self.get('systems/{}/serviceinfo/categories/{}'.format(system_id, category_id),
                              {'systemUnitId': unit_id})

    async def get_category(self,
                           system_id: int,
                           category_id: str,
                           unit_id: int = 0):
        data = await self.get_category_raw(system_id, category_id, unit_id)
        for param in data:
            self.add_parameter_extensions(param)
        return data

    async def get_categories(self,
                             system_id: int,
                             parameters: bool,
                             unit_id: int = 0):
        _LOGGER.debug("Requesting categories on system {}".format(system_id))

        data = await self.get('systems/{}/serviceinfo/categories'.format(system_id),
                              {'parameters'  : str(parameters),
                               'systemUnitId': unit_id})
        for category in data:
            if category['parameters']:
                for param in category['parameters']:
                    self.add_parameter_extensions(param)
        return data

    async def get_status_raw(self, system_id: int):
        _LOGGER.debug("Requesting status on system {}".format(system_id))
        return await self.get('systems/{}/status/system'.format(system_id))

    async def get_status(self, system_id: int):
        data = await self.get_status_raw(system_id)
        for status in data:
            if status['parameters']:
                for param in status['parameters']:
                    self.add_parameter_extensions(param)
        return data

    async def get_units(self, system_id: int):
        _LOGGER.debug("Requesting units on system {}".format(system_id))
        return await self.get('systems/{}/units'.format(system_id))

    async def get_unit_status(self, system_id: int, unit_id: int):
        _LOGGER.debug("Requesting unit {} on system {}".format(unit_id, system_id))
        data = await self.get('systems/{}/status/systemUnit/{}'.format(system_id, unit_id))
        for status in data:
            if status['parameters']:
                for param in status['parameters']:
                    self.add_parameter_extensions(param)
        return data

    async def get_notifications(self,
                                system_id: int,
                                active: bool = True,
                                notifiction_type: str = 'ALARM'):
        _LOGGER.debug("Requesting notifications on system {}".format(system_id))
        params = {
            'active'      : str(active),
            'itemsPerPage': 100,
            'type'        : notifiction_type,
        }
        data = await self.get('systems/{}/notifications'.format(system_id), params=params)
        return data['objects']
