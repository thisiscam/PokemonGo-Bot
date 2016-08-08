import json
import os
import re
import sys
import time
from datetime import datetime
import dateutil
from dateutil import parser as date_parser
import cfscrape

from . import PokemonCatchWorker
from base_task import BaseTask
from utils import distance
from expiringdict import ExpiringDict

class SnipePokemon(BaseTask):
    SUPPORTED_TASK_API_VERSION = 1

    def initialize(self):
        self.api = self.bot.api
        self.pokemon_list = self.bot.pokemon_list
        self.position = self.bot.position
        self.cached_positions = ExpiringDict(max_len=100, max_age_seconds=60 * 5)
        self.scraper = cfscrape.create_scraper()
        self.last_sniping_time = None
        self.snipe_wait_interval = 120

    def work(self):
        if self.bot.config.snipe_list == None:
            return
        if not self.last_sniping_time or (datetime.now() - self.last_sniping_time).total_seconds() > self.snipe_wait_interval:
            self.start_sniping()
            self.last_sniping_time = datetime.now()
        else:
            self.logger.info("{0} seconds till next snipping".format(self.snipe_wait_interval - (datetime.now() - self.last_sniping_time).total_seconds()))
        
    def start_sniping(self):
        try:
            self.logger.info('Reading snipping list.')
            with open(self.bot.config.snipe_list, 'r+') as f:
                try:
                    locations_json = json.load(f)
                except ValueError:
                    self.logger.error('Invalid json file')
                    return

                self.snipe_wait_interval = locations_json.get("snipe_wait_interval", 120)

                try:
                    locations = locations_json['locations']
                except KeyError:
                    self.logger.error('Failed to parse sniping locations')
                    return

                if locations_json.get("use_pokesnipers", False):
                    self.logger.info("Asking for reports from Pokesnipers.com")
                    try:
                        pokesnipers_responses = self.scraper.get("http://pokesnipers.com/api/v1/pokemon.json").json()["results"]
                    except ValueError as e:
                        self.logger.warning("Either Pokesnipers.com is down or they don't got any reports yet")
                        pokesnipers_responses = []
                    pokesnipers_active_responses = []
                    for resp in pokesnipers_responses:
                        until = date_parser.parse(resp["until"])
                        diff = (until - datetime.now(dateutil.tz.tzutc())).total_seconds()
                        if diff > 60 * 1: # focus on pokemon that will still be there after 1 mins 
                            pokesnipers_active_responses.append(resp)
                    self.logger.info("Focusing to {0} number of coordinates, where {1} are spotted!".format(len(pokesnipers_active_responses), ", ".join([r['name'] for r in  pokesnipers_active_responses])))
                    locations = locations + [r['coords'] for r in pokesnipers_active_responses]

                if isinstance(locations, list) and len(locations):
                    while locations:
                        location = locations.pop(0)
                        self.logger.info('Found location: ' + location)
                        location = location.replace(' ', '')
                        pattern = '^(\-?\d+(\.\d+)?),\s*(\-?\d+(\.\d+)?)$'
                        if not re.match(pattern, location):
                            self.logger.error('Wrong format location!')
                            continue
                        if location in self.cached_positions and self.cached_positions[location] == 0:
                            self.logger.info("Ignoring coords {0} because it has been tried recently".format(location))
                            continue
                        elif location not in self.cached_positions:
                            self.cached_positions[location] = 2
                        else:
                            self.cached_positions[location] -= 1
                        self.snipe_pokemon(location)
                        f.seek(0)
                        try:
                            json.dump(locations_json, f)
                        except IOError:
                            self.logger.error('Failed to remove location from snipe list.')
                            return
                        except:
                            self.logger.error('Unknown Error occurred attempting to remove location from snipe list.')
                        f.truncate()
                else:
                    self.logger.warning('No locations to snipe!')
                return
        except IOError:
            self.logger.error('Error reading sniping list!')
            return

    def snipe_pokemon(self, location, delay=2, firstTry=True, prevPosition=None):
        # Check if session token has expired
        self.bot.check_session(self.bot.position[0:2])
        self.bot.heartbeat()

        if firstTry:
            prevPosition = self.bot.position

            # Teleport to location
            self.logger.info('Teleport to location..')
            latitude, longitude = location.split(',')
            self.api.set_position(float(latitude), float(longitude), 0)

        self.cell = self.bot.get_meta_cell()

        catch_pokemon = None
        if 'catchable_pokemons' in self.cell and len(self.cell['catchable_pokemons']) > 0:
            self.logger.info('Something rustles nearby!')
            # Sort all by distance from current pos- eventually this should
            # build graph & A* it
            self.cell['catchable_pokemons'].sort(
                key=
                lambda x: distance(self.position[0], self.position[1], x['latitude'], x['longitude']))


            user_web_catchable = 'web/catchable-%s.json' % (self.bot.config.username)
            for pokemon in self.cell['catchable_pokemons']:
                with open(user_web_catchable, 'w') as outfile:
                    json.dump(pokemon, outfile)

                with open(user_web_catchable, 'w') as outfile:
                    json.dump({}, outfile)

            catch_pokemon = self.cell['catchable_pokemons'][0]

            # Try to catch VIP pokemon
            for pokemon in self.cell['catchable_pokemons']:
                pokemon_num = int(pokemon['pokemon_id']) - 1
                pokemon_name = self.pokemon_list[int(pokemon_num)]['Name']
                vip_name = self.bot.config.vips.get(pokemon_name)
                if vip_name == {}:
                    self.logger.info('Found a VIP pokemon: ' + pokemon_name)
                    catch_pokemon = pokemon

                    # if VIP pokemon is nearest, break loop
                    if pokemon == self.cell['catchable_pokemons'][0]:
                        break

        if 'wild_pokemons' in self.cell and len(self.cell['wild_pokemons']) > 0 and not catch_pokemon:
            # Sort all by distance from current pos- eventually this should
            # build graph & A* it
            self.cell['wild_pokemons'].sort(
                key=
                lambda x: distance(self.position[0], self.position[1], x['latitude'], x['longitude']))
            catch_pokemon = self.cell['wild_pokemons'][0]

        if not catch_pokemon:
            if firstTry:
                self.logger.warning("No pokemon found! But sometimes this could be a bug, let's retry for once immediately")
                time.sleep(delay * 2)
                return self.snipe_pokemon(location, delay, firstTry=False, prevPosition=prevPosition)
            else:
                self.logger.warning("No pokemon found. Giving up")
                # go back
                self.api.set_position(*prevPosition)
                time.sleep(delay)
                self.bot.heartbeat()
                return None

        catchWorker = PokemonCatchWorker(catch_pokemon, self.bot)

        self.logger.info('Encounter pokemon')
        apiEncounterResponse = catchWorker.create_encounter_api_call()

        time.sleep(delay)
        # go back
        self.logger.info('Teleport back previous location..')
        self.api.set_position(*prevPosition)
        # wait for go back
        time.sleep(delay)
        self.bot.heartbeat()

        # Catch 'em all
        catchWorker.work(apiEncounterResponse)
