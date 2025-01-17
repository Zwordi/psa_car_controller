import json
import threading
from json import JSONEncoder
from hashlib import md5
from time import sleep

from oauth2_client.credentials_manager import ServiceInformation
from urllib3.exceptions import InvalidHeader

import psa_connectedcar as psac
from libs.car import Cars, Car
from libs.charging import Charging
from libs.psa.AccountInformation import AccountInformation
from libs.psa.RemoteClient import RemoteClient
from libs.psa.RemoteCredentials import RemoteCredentials
from libs.psa.oauth import OpenIdCredentialManager, Oauth2PSACCApiConfig, OauthAPIClient
from ecomix import Ecomix
from libs.psa.constants import realm_info, AUTHORIZE_SERVICE
from psa_connectedcar.rest import ApiException
from mylogger import logger

from web.abrp import Abrp
from web.db import Database

SCOPE = ['openid profile']
CARS_FILE = "cars.json"
DEFAULT_CONFIG_FILENAME = "config.json"


class MyPSACC:
    def connect(self, user, password):
        self.manager.init_with_user_credentials_realm(user, password, self.realm)

    # pylint: disable=too-many-arguments
    def __init__(self, refresh_token, client_id, client_secret, remote_refresh_token, customer_id, realm, country_code,
                 proxies=None, weather_api=None, abrp=None, co2_signal_api=None):
        self.realm = realm
        self.service_information = ServiceInformation(AUTHORIZE_SERVICE,
                                                      realm_info[self.realm]['oauth_url'],
                                                      client_id,
                                                      client_secret,
                                                      SCOPE, False)
        self.client_id = client_id
        self.manager = OpenIdCredentialManager(self.service_information)
        self.api_config = Oauth2PSACCApiConfig()
        self.api_config.set_refresh_callback(self.manager.refresh_token_now)
        self.manager.refresh_token = refresh_token
        self.account_info = AccountInformation(client_id, customer_id, realm, country_code)
        self.remote_access_token = None
        self.vehicles_list = Cars.load_cars(CARS_FILE)
        self.customer_id = customer_id
        self._config_hash = None
        self.api_config.verify_ssl = False
        self.api_config.api_key['client_id'] = self.client_id
        self.api_config.api_key['x-introspect-realm'] = self.realm
        self.remote_token_last_update = None
        self._record_enabled = False
        self.weather_api = weather_api
        self.country_code = country_code
        self.info_callback = []
        self.info_refresh_rate = 120
        if abrp is None:
            self.abrp = Abrp()
        else:
            self.abrp: Abrp = Abrp(**abrp)
        self.set_proxies(proxies)
        self.config_file = DEFAULT_CONFIG_FILENAME
        Ecomix.co2_signal_key = co2_signal_api
        self.refresh_thread = None
        remote_credentials = RemoteCredentials(remote_refresh_token)
        remote_credentials.update_callbacks.append(self.save_config)
        self.remote_client = RemoteClient(self.account_info,
                                          self.vehicles_list,
                                          self.manager,
                                          remote_credentials)

    def get_app_name(self):
        return realm_info[self.realm]['app_name']

    def api(self) -> psac.VehiclesApi:
        self.api_config.access_token = self.manager.access_token
        api_instance = psac.VehiclesApi(OauthAPIClient(self.api_config))
        return api_instance

    def set_proxies(self, proxies):
        if proxies is None:
            proxies = dict(http='', https='')
            self.api_config.proxy = None
        else:
            self.api_config.proxy = proxies['http']
            self.abrp.proxies = proxies
        self.manager.proxies = proxies

    def get_vehicle_info(self, vin, cache=False):
        res = None
        car = self.vehicles_list.get_car_by_vin(vin)
        if cache and car.status is not None:
            res = car.status
        else:
            for _ in range(0, 2):
                try:
                    res = self.api().get_vehicle_status(car.vehicle_id, extension=["odometer"])
                    if res is not None:
                        car.status = res
                        if self._record_enabled:
                            self.record_info(car)
                        return res
                except (ApiException, InvalidHeader) as ex:
                    logger.error("get_vehicle_info: ApiException: %s", ex, exc_info_debug=True)
            car.status = res
        return res

    def __refresh_vehicle_info(self):
        if self.info_refresh_rate is not None:
            while True:
                try:
                    logger.debug("refresh_vehicle_info")
                    for car in self.vehicles_list:
                        self.get_vehicle_info(car.vin)
                    for callback in self.info_callback:
                        callback()
                except:  # pylint: disable=bare-except
                    logger.exception("refresh_vehicle_info: ")
                sleep(self.info_refresh_rate)

    def start_refresh_thread(self):
        if self.refresh_thread is None:
            self.refresh_thread = threading.Thread(target=self.__refresh_vehicle_info)
            self.refresh_thread.setDaemon(True)
            self.refresh_thread.start()

    def get_vehicles(self):
        try:
            res = self.api().get_vehicles_by_device()
            for vehicle in res.embedded.vehicles:
                self.vehicles_list.add(Car(vehicle.vin, vehicle.id, vehicle.brand, vehicle.label))
            self.vehicles_list.save_cars()
        except (ApiException, InvalidHeader):
            logger.exception("get_vehicles:")
        return self.vehicles_list

    def get_charge_status(self, vin):
        data = self.get_vehicle_info(vin)
        status = data.get_energy('Electric').charging.status
        return status

    def save_config(self, name=None, force=False):
        if name is None:
            name = self.config_file
        config_str = json.dumps(self, cls=MyPeugeotEncoder, sort_keys=True, indent=4).encode("utf8")
        new_hash = md5(config_str).hexdigest()
        if force or self._config_hash != new_hash:
            with open(name, "wb") as f:
                f.write(config_str)
            self._config_hash = new_hash
            logger.info("save config change")

    @staticmethod
    def load_config(name="config.json"):
        with open(name, "r", encoding="utf-8") as f:
            config_str = f.read()
            config = dict(**json.loads(config_str))
            if "country_code" not in config:
                config["country_code"] = input("What is your country code ? (ex: FR, GB, DE, ES...)\n")
            for new_el in ["abrp", "co2_signal_api"]:
                if new_el not in config:
                    config[new_el] = None
            psacc = MyPSACC(**config)
            psacc.config_file = name
            return psacc

    def set_record(self, value: bool):
        self._record_enabled = value

    def record_info(self, car: Car):
        mileage = car.status.timed_odometer.mileage
        level = car.status.get_energy('Electric').level
        level_fuel = car.status.get_energy('Fuel').level
        charge_date = car.status.get_energy('Electric').updated_at
        moving = car.status.kinetic.moving

        longitude = car.status.last_position.geometry.coordinates[0]
        latitude = car.status.last_position.geometry.coordinates[1]
        altitude = car.status.last_position.geometry.coordinates[2]
        date = car.status.last_position.properties.updated_at
        if date is None:
            date = charge_date
        logger.debug("vin:%s longitude:%s latitude:%s date:%s mileage:%s level:%s charge_date:%s level_fuel:"
                     "%s moving:%s", car.vin, longitude, latitude, date, mileage, level, charge_date, level_fuel,
                     moving)
        Database.record_position(self.weather_api, car.vin, mileage, latitude, longitude, altitude, date, level,
                                 level_fuel, moving)
        self.abrp.call(car, Database.get_last_temp(car.vin))
        try:
            charging_status = car.status.get_energy('Electric').charging.status
            charging_mode = car.status.get_energy('Electric').charging.charging_mode
            charging_rate = car.status.get_energy('Electric').charging.charging_rate
            autonomy = car.status.get_energy('Electric').autonomy
            Charging.record_charging(car, charging_status, charge_date, level, latitude, longitude, self.country_code,
                                     charging_mode, charging_rate, autonomy)
            logger.debug("charging_status:%s ", charging_status)
        except AttributeError:
            logger.error("charging status not available from api")

    def __iter__(self):
        for key, value in self.__dict__.items():
            yield key, value


class MyPeugeotEncoder(JSONEncoder):

    def default(self, mp: MyPSACC):  # pylint: disable=arguments-renamed
        mpd = {"proxies": mp.manager.proxies,
               "refresh_token": mp.manager.refresh_token,
               "client_secret": mp.service_information.client_secret,
               "abrp": dict(mp.abrp),
               "remote_refresh_token": mp.remote_client.remoteCredentials.refresh_token,
               "customer_id": mp.account_info.customer_id,
               "client_id": mp.account_info.client_id,
               "realm": mp.account_info.realm,
               "country_code": mp.account_info.country_code,
               "weather_api": mp.weather_api,
               "co2_signal_api": Ecomix.co2_signal_key
               }
        return mpd
