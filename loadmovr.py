#!/usr/bin/python

from movr import MovR
from generators import MovRGenerator
import argparse
import sys, os, time, datetime, random, math, signal, threading, re
import logging
from faker import Faker
from models import User, Vehicle, Ride
from cockroachdb.sqlalchemy import run_transaction
from sqlalchemy.orm import sessionmaker
from sqlalchemy import create_engine

RUNNING_THREADS = []
TERMINATE_GRACEFULLY = False

def signal_handler(sig, frame):
    global TERMINATE_GRACEFULLY
    grace_period = 15
    logging.info('Waiting at most %d seconds for threads to shutdown...', grace_period)
    TERMINATE_GRACEFULLY = True

    start = time.time()
    while threading.active_count() > 1:
        if (time.time() - start) > grace_period:
            logging.info("grace period has passed. killing threads.")
            os._exit(1)
        else:
            time.sleep(.1)

    logging.info("shutting down gracefully.")
    sys.exit(0)




DEFAULT_PARTITION_MAP = {
    "us_east": ["new york", "boston", "washington dc"],
    "us_west": ["san francisco", "seattle", "los angeles"],
    "eu_west": ["amsterdam", "paris", "rome"]
}

# Create a connection to the movr database and populate a set of cities with rides, vehicles, and users.
def load_movr_data(conn_string, num_users, num_vehicles, num_rides, cities, echo_sql = False):
    if num_users <= 0 or num_rides <= 0 or num_vehicles <= 0:
        raise ValueError("The number of objects to generate must be > 0")

    start_time = time.time()
    with MovR(conn_string, echo=echo_sql) as movr:
        engine = create_engine(conn_string, convert_unicode=True, echo=echo_sql)
        for city in cities:
            if TERMINATE_GRACEFULLY:
                logging.debug("terminating")
                break

            logging.info("Generating user data for %s...", city)
            add_users(engine, num_users, city)
            logging.info("Generating vehicle data for %s...", city)
            add_vehicles(engine, num_vehicles, city)
            logging.info("Generating ride data for %s...", city)
            add_rides(engine, num_rides, city)

            logging.info("populated %s in %f seconds",
                  city, time.time() - start_time)

    return

# Generates evenly distributed load among the provided cities
def simulate_movr_load(conn_string, cities, movr_objects, active_rides, read_percentage, echo_sql = False):

    datagen = Faker()

    with MovR(conn_string, echo=echo_sql) as movr:
        while True:

            if TERMINATE_GRACEFULLY:
                logging.debug("Terminating thread.")
                return

            active_city = random.choice(cities)

            if random.random() < read_percentage:
                # simulate user loading screen
                movr.get_vehicles(active_city,25)
            else:
                #do write operations randomly
                if random.random() < .1:
                    # simulate new signup
                    movr_objects[active_city]["users"].append(movr.add_user(active_city, datagen.name(), datagen.address(), datagen.credit_card_number()))
                elif random.random() < .1:
                    # simulate a user adding a new vehicle to the population
                    movr_objects[active_city]["vehicles"].append(
                        movr.add_vehicle(active_city,
                                        owner_id = random.choice(movr_objects[active_city]["users"])['id'],
                                        type = MovRGenerator.generate_random_vehicle(),
                                        vehicle_metadata = MovRGenerator.generate_vehicle_metadata(type),
                                        status=MovRGenerator.get_vehicle_availability(),
                                        current_location = datagen.address()))
                elif random.random() < .5:
                    # simulate a user starting a ride
                    ride = movr.start_ride(active_city, random.choice(movr_objects[active_city]["users"])['id'],
                                           random.choice(movr_objects[active_city]["vehicles"])['id'])
                    active_rides.append(ride)
                else:
                    if len(active_rides):
                        #simulate a ride ending
                        ride = active_rides.pop()
                        movr.end_ride(ride['city'], ride['id'])



# creates a map of partions when given a list of pairs in the form <partition>:<city_id>.
def extract_partition_pairs_from_cli(pair_list):
    if pair_list is None:
        return DEFAULT_PARTITION_MAP

    partition_pairs = {}

    for partition_pair in pair_list:
        pair = partition_pair.split(":")
        if len(pair) < 1:
            pair = ["default"].append(pair[0])
        else:
            pair = [pair[0], ":".join(pair[1:])]  # if there are many semicolons convert this to only two items


        if pair[0] in partition_pairs:
            partition_pairs[pair[0]].append(pair[1])
        else:
            partition_pairs[pair[0]] = [pair[1]]

    return partition_pairs

def setup_parser():
    parser = argparse.ArgumentParser(description='CLI for MovR.')
    subparsers = parser.add_subparsers(dest='subparser_name')

    ###########
    # GENERAL COMMANDS
    ##########
    parser.add_argument('--num-threads', dest='num_threads', type=int, default=5,
                            help='The number threads to use for MovR (default =5)')
    parser.add_argument('--log-level', dest='log_level', default='info',
                        help='The log level ([debug|info|warning|error]) for MovR messages. (default = info)')
    parser.add_argument('--url', dest='conn_string', default='postgres://root@localhost:26257/movr?sslmode=disable',
                        help="connection string to movr database. Default is 'postgres://root@localhost:26257/movr?sslmode=disable'")

    parser.add_argument('--echo-sql', dest='echo_sql', action='store_true',
                        help='set this if you want to print all executed SQL statements')

    ###############
    # LOAD COMMANDS
    ###############
    load_parser = subparsers.add_parser('load', help="load movr data into a database")
    load_parser.add_argument('--num-users', dest='num_users', type=int, default=50,
                             help='The number of random users to add to the dataset')
    load_parser.add_argument('--num-vehicles', dest='num_vehicles', type=int, default=10,
                             help='The number of random vehicles to add to the dataset')
    load_parser.add_argument('--num-rides', dest='num_rides', type=int, default=500,
                             help='The number of random rides to add to the dataset')
    load_parser.add_argument('--partition-by', dest='partition_pair', action='append',
                             help='Pairs in the form <partition>:<city_id> that will be used to enable geo-partitioning. Example: us_west:seattle. Use this flag multiple times to add multiple cities.')
    load_parser.add_argument('--enable-geo-partitioning', dest='enable_geo_partitioning', action='store_true',
                             help='Set this if your cluster has an enterprise license (https://cockroa.ch/2BoAlgB) and you want to use geo-partitioning functionality (https://cockroa.ch/2wd96zF)')
    load_parser.add_argument('--skip-init', dest='skip_reload_tables', action='store_true',
                             help='Keep existing tables and data when loading Movr tables')

    ###############
    # RUN COMMANDS
    ###############
    run_parser = subparsers.add_parser('run', help="generate fake traffic for the movr database")

    run_parser.add_argument('--city', dest='city', action='append',
                            help='The names of the cities to use when generating load. Use this flag multiple times to add multiple cities.')
    run_parser.add_argument('--read-only-percentage', dest='read_percentage', type=float,
                            help='Value between 0-1 indicating how many simulated read-only home screen loads to perform as a percentage of overall activities',
                            default=.95)

    return parser

##############
# BULK DATA LOADING
##############

def add_rides(engine, num_rides, city):
    chunk_size = 800
    datagen = Faker()

    def add_rides_helper(sess, chunk, n):
        users = sess.query(User).filter_by(city=city).all()
        vehicles = sess.query(Vehicle).filter_by(city=city).all()

        rides = []
        for i in range(chunk, min(chunk + chunk_size, num_rides)):
            start_time = datetime.datetime.now() - datetime.timedelta(days=random.randint(0, 30))
            rides.append(Ride(id=MovRGenerator.generate_uuid(),
                              city=city,
                              vehicle_city=city,
                              rider_id=random.choice(users).id,
                              vehicle_id=random.choice(vehicles).id,
                              start_time=start_time,
                              start_address=datagen.address(),
                              end_address=datagen.address(),
                              revenue=MovRGenerator.generate_revenue(),
                              end_time=start_time + datetime.timedelta(minutes=random.randint(0, 60))))
        sess.bulk_save_objects(rides)

    for chunk in range(0, num_rides, chunk_size):
        run_transaction(sessionmaker(bind=engine),
                        lambda s: add_rides_helper(s, chunk, min(chunk + chunk_size, num_rides)))

def add_users(engine, num_users, city):
    chunk_size = 1000
    datagen = Faker()

    def add_users_helper(sess, chunk, n):
        users = []
        for i in range(chunk, n):
            users.append(User(id=MovRGenerator.generate_uuid(),
                              city=city,
                              name=datagen.name(),
                              address=datagen.address(),
                              credit_card=datagen.credit_card_number()))
        sess.bulk_save_objects(users)

    for chunk in range(0, num_users, chunk_size):
        run_transaction(sessionmaker(bind=engine),
                        lambda s: add_users_helper(s, chunk, min(chunk + chunk_size, num_users)))

def add_vehicles(engine, num_vehicles, city):
    chunk_size = 1000
    def add_vehicles_helper(sess, chunk, n):
        owners = sess.query(User).filter_by(city=city).all()
        vehicles = []
        for i in range(chunk, n):
            vehicle_type = MovRGenerator.generate_random_vehicle()
            vehicles.append(Vehicle(id=MovRGenerator.generate_uuid(),
                                    type=vehicle_type,
                                    city=city,
                                    owner_id=(random.choice(owners)).id,
                                    status=MovRGenerator.get_vehicle_availability(),
                                    ext=MovRGenerator.generate_vehicle_metadata(vehicle_type)))
        sess.bulk_save_objects(vehicles)

    for chunk in range(0, num_vehicles, chunk_size):
        run_transaction(sessionmaker(bind=engine),
                        lambda s: add_vehicles_helper(s, chunk, min(chunk + chunk_size, num_vehicles)))

def run_data_loader(conn_string, num_users, num_rides, num_vehicles, num_threads,
                    skip_reload_tables, echo_sql, enable_geo_partitioning):
    if num_users <= 0 or num_rides <= 0 or num_vehicles <= 0:
        raise ValueError("The number of objects to generate must be > 0")

    start_time = time.time()
    with MovR(conn_string, init_tables=(not skip_reload_tables), echo=echo_sql) as movr:
        if enable_geo_partitioning:
            movr.add_geo_partitioning(partition_city_map)

        logging.info("loading cities %s", all_cities)
        logging.info("loading movr data with ~%d users, ~%d vehicles, and ~%d rides",
                     num_users, num_vehicles, num_rides)

    usable_threads = min(num_threads, len(all_cities))  # don't create more than 1 thread per city
    if usable_threads < num_threads:
        logging.info("Only using %d of %d requested threads, since we only create at most one thread per city",
                     usable_threads, num_threads)

    cities_per_thread = int(math.ceil((float(len(all_cities)) / usable_threads)))
    num_users_per_city = int(math.ceil((float(num_users) / len(all_cities))))
    num_rides_per_city = int(math.ceil((float(num_rides) / len(all_cities))))
    num_vehicles_per_city = int(math.ceil((float(num_vehicles) / len(all_cities))))

    cities_to_load = all_cities

    RUNNING_THREADS = []

    for i in range(usable_threads):
        if len(cities_to_load) > 0:
            t = threading.Thread(target=load_movr_data, args=(conn_string, num_users_per_city, num_vehicles_per_city,
                                                              num_rides_per_city, cities_to_load[:cities_per_thread],
                                                              echo_sql))
            cities_to_load = cities_to_load[cities_per_thread:]
            t.start()
            RUNNING_THREADS.append(t)

    while threading.active_count() > 1:  # keep main thread alive so we can catch ctrl + c
        time.sleep(0.1)

    duration = time.time() - start_time

    logging.info("populated %s cities in %f seconds", len(all_cities), duration)
    logging.info("- %f users/second", float(num_users_per_city * len(all_cities)) / duration)
    logging.info("- %f rides/second", float(num_rides_per_city * len(all_cities)) / duration)
    logging.info("- %f vehicles/second", float(num_vehicles_per_city * len(all_cities)) / duration)

# generate fake load for objects within the provided city list
def run_load_generator(conn_string, read_percentage, city_list, echo_sql, num_threads):
    if read_percentage < 0 or read_percentage > 1:
        raise ValueError("read percentage must be between 0 and 1")

    cities = all_cities if city_list is None else city_list
    logging.info("simulating movr load for cities %s", cities)

    movr_objects = {}

    logging.info("warming up....")
    with MovR(conn_string, echo=echo_sql) as movr:
        active_rides = []
        for city in cities:
            movr_objects[city] = {"users": movr.get_users(city), "vehicles": movr.get_vehicles(city)}
            if len(list(movr_objects[city]["vehicles"])) == 0 or len(list(movr_objects[city]["users"])) == 0:
                logging.error("must have users and vehicles for city '%s' in the movr database to generate load. try running with the 'load' command.", city)
                sys.exit(1)
            active_rides.extend(movr.get_active_rides(city))

    logging.info("starting load")
    RUNNING_THREADS = []
    for i in range(num_threads):
        t = threading.Thread(target=simulate_movr_load, args=(conn_string, cities, movr_objects,
                                                    active_rides, read_percentage, echo_sql ))
        t.start()
        RUNNING_THREADS.append(t)

    while True: #keep main thread alive to catch exit signals
        time.sleep(0.5)

if __name__ == '__main__':

    # support ctrl + c for exiting multithreaded operation
    signal.signal(signal.SIGINT, signal_handler)

    args = setup_parser().parse_args()

    if not re.search('.*26257/(.*)\?', args.conn_string):
        logging.error("The connection string needs to point to a database. Example: postgres://root@localhost:26257/mymovrdatabase?sslmode=disable")
        sys.exit(1)

    if args.num_threads <= 0:
        logging.error("Number of threads must be greater than 0.")
        sys.exit(1)

    if args.log_level not in ['debug', 'info', 'warning', 'error']:
        logging.error("Invalid log level: %s", args.log_level)
        sys.exit(1)

    level_map = {
        'debug': logging.DEBUG,
        'info': logging.INFO,
        'warning': logging.WARNING,
        'error': logging.ERROR
    }

    logging.basicConfig(level=level_map[args.log_level],
                        format='[%(levelname)s] (%(threadName)-10s) %(message)s', )


    logging.info("connected to movr database @ %s" % args.conn_string)

    #format connection string to work with our cockroachdb driver.
    conn_string = args.conn_string.replace("postgres://", "cockroachdb://")
    conn_string = conn_string.replace("postgresql://", "cockroachdb://")

    # population partitions
    partition_city_map = extract_partition_pairs_from_cli(args.partition_pair if args.subparser_name=='load' else None)

    all_cities = []
    for partition in partition_city_map:
        all_cities += partition_city_map[partition]

    if args.subparser_name=='load':
        run_data_loader(conn_string, args.num_users, args.num_rides, args.num_vehicles, args.num_threads,
                        args.skip_reload_tables, args.echo_sql, args.enable_geo_partitioning)
    else:
        run_load_generator(conn_string, args.read_percentage, args.city, args.echo_sql, args.num_threads)





