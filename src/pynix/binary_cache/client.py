"""Module for interacting with a running servenix instance."""
import argparse
from copy import copy
from datetime import datetime
import getpass
import gzip
from io import BytesIO
import json
import logging
import os
from os.path import (join, exists, isdir, isfile, expanduser, basename,
                     getmtime, dirname)
import re
import shutil
from subprocess import (Popen, PIPE, check_output, CalledProcessError,
                        check_call, call)
import sys
import tarfile
import tempfile
from threading import Thread, RLock, BoundedSemaphore
from six.moves.urllib_parse import urlparse
from concurrent.futures import ThreadPoolExecutor, Future, wait, as_completed
from multiprocessing import cpu_count
import yaml
import gzip
from urllib.parse import urljoin

# Special-case here to address a runtime bug I've encountered
try:
    import sqlite3
except ImportError as err:
    if "does not define init" in str(err):
        exit("Could not import sqlite3. This is probably due to PYTHONPATH "
             "corruption: make sure your PYTHONPATH is empty prior to "
             "running this command.")
    else:
        raise
import time

import magic
import requests
import six

from pynix import __version__
from pynix.utils import (strip_output, decode_str, NIX_STORE_PATH,
                         NIX_STATE_PATH, NIX_DB_PATH, nix_cmd,
                         query_store, instantiate, tell_size,
                         is_path_in_store, format_seconds)
from pynix.exceptions import (CouldNotConnect, NixImportFailed, CliError,
                              ObjectNotBuilt, NixBuildError, NoSuchObject,
                              OperationNotSupported)
from pynix.binary_cache.nix_info_caches import PathReferenceCache
from pynix.narinfo import (NarInfo, resolve_compression_type,
                           COMPRESSION_TYPES, COMPRESSION_TYPE_ALIASES)
from pynix.build import needed_to_build_multi, parse_deriv_paths

NIX_PATH_CACHE = os.environ.get("NIX_PATH_CACHE",
                                expanduser("~/.nix-path-cache"))
NIX_NARINFO_CACHE = os.environ.get("NIX_NARINFO_CACHE",
                                   expanduser("~/.nix-narinfo-cache"))
ENDPOINT_REGEX = re.compile(r"https?://([\w_-]+)(\.[\w_-]+)*(:\d+)?$")

# Limit of how many paths to show, so the screen doesn't flood.
SHOW_PATHS_LIMIT = int(os.environ.get("SHOW_PATHS_LIMIT", 25))

# Mimetypes of tarball files
TARBALL_MIMETYPES = set(['application/x-gzip', 'application/x-xz',
                         'application/x-bzip2', 'application/zip'])

class NixCacheClient(object):
    """Wraps some state for sending store objects."""
    def __init__(self, endpoint, dry_run=False, username=None, password=None,
                 cache_location=None, cache_enabled=True, send_nars=False,
                 compression_type="xz", use_batch_fetching=True,
                 max_jobs=cpu_count(), max_attempts=3):
        #: Server running servenix (string).
        if endpoint:
            self._endpoint = endpoint
        else:
            self._endpoint = None
        #: Base name of server (for caching).
        self._endpoint_server = urlparse(endpoint).netloc
        #: If true, no actual paths will be sent/fetched/built.
        self._dry_run = dry_run
        #: If not none, will use to authenticate with the repo.
        if username is not None:
            self._username = username
        elif os.environ.get("NIX_BINARY_CACHE_USERNAME", "") != "":
            self._username = os.environ["NIX_BINARY_CACHE_USERNAME"]
        else:
            self._username = None
        #: Ignored if username is None.
        self._password = password
        #: Set at a later point, if username is not None.
        self._auth = None
        #: Used to avoid unnecessary overhead in handshakes etc.
        self._session = None
        #: Set of paths known to exist on the server already (set of strings).
        self._objects_on_server = set()
        #: When sending objects, this can be used to count remaining.
        self._remaining_objects = None
        # A thread pool which handles queries for narinfo.
        self._query_pool = ThreadPoolExecutor(max_workers=max_jobs)
        # A thread pool which handles store object fetches.
        self._fetch_pool = ThreadPoolExecutor(max_workers=max_jobs)
        #: Cache of narinfo objects requested from the server.
        self._narinfo_cache = {}
        #: This will get filled up as we fetch paths; it lets avoid repeats.
        self._paths_fetched = set()
        self._max_jobs = max_jobs
        # A dictionary mapping nix store paths to futures fetching
        # those paths from a cache. Each fetch happens in a different
        # thread, and we use this dictionary to make sure that a fetch
        # only happens once.
        self._fetch_futures = {}
        # A lock which syncronizes access to the fetch state.
        self._fetch_lock = RLock()
        # Will be set to a non-None value when fetching.
        self._fetch_total = None
        # Connection to the nix state database.
        self._db_con = sqlite3.connect(NIX_DB_PATH)
        # Caches nix path references.
        self._reference_cache = PathReferenceCache(
            db_con=self._db_con, create_db_con_each_time=True)
        # How many times to attempt fetching a package.
        self._max_attempts = max_attempts
        # Will be set to true if there's an interruption of some kind.
        self._cancelled = False
        # Whether to send NARs when uploading
        self._send_nars = send_nars
        # How to compress NARs send during uploading
        self._compression_type = resolve_compression_type(compression_type)
        # Whether to use batch fetching when available
        self._use_batch_fetching = use_batch_fetching

    def _update_narinfo_cache(self, narinfo, write_to_disk):
        """Write a narinfo entry to the cache.

        :param narinfo: Information about a nix archive.
        :type narinfo: :py:class:`NarInfo`
        :param write_to_disk: Write to the on-disk cache.
        :type write_to_disk: ``bool``
        """
        path = narinfo.store_path
        self._narinfo_cache[path] = narinfo
        if write_to_disk is False:
            return
        # The narinfo cache is indexed by the server name of the endpoint.
        server_cache = join(NIX_NARINFO_CACHE, self._endpoint_server)
        narinfo_path = join(server_cache, basename(path))
        if not isdir(server_cache):
            os.makedirs(server_cache)
        if isfile(narinfo_path):
            return
        tempfile_fd, tempfile_path = tempfile.mkstemp()
        with os.fdopen(tempfile_fd, "w") as f:
            f.write(json.dumps(narinfo.to_dict()))
        shutil.move(tempfile_path, narinfo_path)

    def get_narinfo(self, path):
        """Request narinfo from a server. These are cached in memory.

        :param path: Store path that we want info on.
        :type path: ``str``

        :return: Information on the archived path.
        :rtype: :py:class:`NarInfo`
        """
        path = join(NIX_STORE_PATH, path)
        if path not in self._narinfo_cache:
            write_to_disk = True
            cache_path = join(NIX_NARINFO_CACHE, self._endpoint_server,
                              basename(path))
            if isfile(cache_path):
                try:
                    logging.debug("Loading {} narinfo from on-disk cache"
                                  .format(basename(path)))
                    with open(cache_path) as f:
                        narinfo = NarInfo.from_dict(json.load(f))
                    write_to_disk = False
                except json.decoder.JSONDecodeError:
                    logging.debug("Invalid cache JSON: {}".format(cache_path))
                    os.unlink(cache_path)
                    return self.get_narinfo(path)
            else:
                logging.debug("Requesting {} narinfo from server"
                              .format(basename(path)))
                prefix = basename(path).split("-")[0]
                url = "{}/{}.narinfo".format(self._endpoint, prefix)
                logging.debug("hitting url {} (for path {})..."
                              .format(url, path))
                response = self._request(url)
                logging.debug("response arrived from {}".format(url))
                narinfo = NarInfo.from_string(response.content)
            self._update_narinfo_cache(narinfo, write_to_disk)
        return self._narinfo_cache[path]

    def get_references(self, path, query_server=False):
        """Get a path's direct references.

        :param path: A nix store path. It must either exist in the
                     local nix store or be available in the binary cache.
        :type path: ``str``
        :param query_server: If true, will attempt to query the server
                             for the paths if not on disk. This is
                             used when fetching a path.
        :type query_server: ``bool``

        :return: A list of absolute paths that the path refers to directly.
        :rtype: ``list`` of ``str``
        """
        try:
            return self._reference_cache.get_references(
                path, hide_stderr=query_server)
        except NoSuchObject as err:
            if query_server is False:
                logging.error("Couldn't determine the references of {} "
                              "locally, and can't query the server"
                              .format(path))
                raise
        narinfo = self.get_narinfo(path)
        refs = [r for r in narinfo.abs_references if r != path]
        self._reference_cache.record_references(narinfo.store_path, refs)
        return refs

    def query_paths(self, paths):
        """Given a list of paths, see which the server has.

        :param paths: A list of nix store paths.
        :type paths: ``iterable`` of ``str``

        :return: A dictionary mapping store paths to booleans (True if
                 on the server, False otherwise).
        :rtype: ``dict`` of ``str`` to ``bool``
        """
        paths = list(set(paths))
        if len(paths) == 0:
            # No point in making a request if we don't have any paths.
            return {}
        url = "{}/query-paths".format(self._endpoint)
        data = json.dumps(paths)
        headers = {"Content-Type": "application/json"}
        logging.debug("Asking the server about {} paths.".format(len(paths)))
        try:
            response = self._connect().get(url, headers=headers, data=data)
            response.raise_for_status()
            return response.json()
        except requests.HTTPError as err:
            if err.response.status_code != 404:
                raise
            logging.warn("Endpoint {} does not support the /query-paths "
                         "route. Querying paths individually."
                         .format(self._endpoint))
            futures = {
                path: self._query_pool.submit(self.query_path_individually,
                                              path)
                for path in paths
            }
            result = {path: fut.result() for path, fut in futures.items()}
            return result

    def query_path_individually(self, path):
        """Send an individual query (.narinfo) for a store path.

        :param path: A store path, to ask a binary cache about.
        :type path: ``str``

        :return: True if the server has the path, and otherwise false.
        :rtype: ``bool``
        """
        logging.debug("Querying for path {}".format(path))
        prefix = basename(path).split("-")[0]
        url = "{}/{}.narinfo".format(self._endpoint, prefix)
        resp = self._connect().get(url)
        has_path = resp.status_code == 200
        if has_path:
            logging.debug("{} has path {}".format(self._endpoint, path))
        else:
            logging.debug("{} does not have path {}"
                          .format(self._endpoint, path))
        return has_path

    def query_path_closures(self, paths, include_nars=False):
        """Given a list of paths, compute their whole closure and ask
        the server which of those paths it has.

        :param paths: A list of store paths.
        :type paths: ``list`` of ``str``
        :param include_nars: If true, will also compute the paths of
        NARs which would be sent.

        :return: The full set of paths that will be sent.
        :rtype: ``set`` of ``str``

        Side effects:
        * Adds 0 or more paths to `self._objects_on_server`.
        """
        paths = [os.path.join(NIX_STORE_PATH, p) for p in paths]
        total = len(paths)
        step = max(total // 10, 1)
        full_path_set = set()
        counts = [0]
        def recur(_paths):
            """Loop for DFS'ing through the paths to generate full closures."""
            for path in _paths:
                if path not in full_path_set:
                    counts[0] += 1
                    recur(self.get_references(path))
                    full_path_set.add(path)
        logging.info("Computing path closure...")
        recur(paths)
        if len(full_path_set) > total:
            logging.info("{} {} given as input, but the full "
                         "dependency closure contains {} paths."
                         .format(total,
                                 "path was" if total == 1 else "paths were",
                                 len(full_path_set)))

        # Now that we have the full list built up, send it to the
        # server to see which paths are already there.
        on_server = self.query_paths(full_path_set)

        # The set of paths that will be sent.
        to_send = set()

        # Store all of the paths which are listed as `True` (exist on
        # the server) in our cache.
        for path, is_on_server in six.iteritems(on_server):
            if is_on_server is True:
                self._objects_on_server.add(path)
            else:
                to_send.add(path)
        return to_send

    def _connect(self, first_time=True, attempts=5):
        """Connect to a binary cache.

        Serves two purposes: verifying that the client can
        authenticate with the cache, and that the binary cache store
        directory matches the client's.

        If password isn't set, reads the NIX_BINARY_CACHE_PASSWORD
        variable for the password. If it is not set, the user will
        be prompted.

        :param first_time: Whether this is the first time it's being
            called, so that we can tailor the error messaging.
        :type first_time: ``bool``

        :param attempts: How many more times to try connecting
        :type  attempts: ``int``

        :return: Either None or a Session object.
        :rtype: ``NoneType`` or :py:class:`requests.sessions.Session`

        :raises: :py:class:`CouldNotConnect` if authentication fails.

        Side effects:
        * Will set the NIX_BINARY_CACHE_{USERNAME,PASSWORD} variables.
        """
        if self._session is not None:
            # Cache to avoid repeated prompts
            return self._session
        if self._password is not None:
            password = self._password
        elif self._username is None:
            password = None
        elif os.environ.get("NIX_BINARY_CACHE_PASSWORD", "") != "":
            logging.debug("Using value in NIX_BINARY_CACHE_PASSWORD variable")
            password = os.environ["NIX_BINARY_CACHE_PASSWORD"]
        elif sys.stdin.isatty():
            prompt = ("Please enter the \033[1mpassword\033[0m for {}: "
                      .format(self._username))
            password = getpass.getpass(prompt)
        else:
            logging.warn("Can't get password for user {}. Auth may fail."
                         .format(self._username))
        if self._username is not None:
            logging.info("Connecting as user {}".format(self._username))
        else:
            logging.info("Connecting...")
        if self._username is not None:
            auth = requests.auth.HTTPBasicAuth(self._username, password)
        else:
            auth = None
        # Create a session. Don't set it on the object yet.
        session = requests.Session()
        # Perform the actual request. See if we get a 200 back.
        url = "{}/nix-cache-info".format(self._endpoint)
        resp = session.get(url, auth=auth)
        if resp.status_code == 200:
            nix_cache_info = yaml.load(resp.content)
            cache_store_dir = nix_cache_info["StoreDir"]
            if cache_store_dir != NIX_STORE_PATH:
                raise ValueError("This binary cache serves packages from "
                                 "store directory {}, but this client is "
                                 "using {}"
                                 .format(cache_store_dir, NIX_STORE_PATH))
            logging.info("Successfully connected to {}".format(self._endpoint))
            self._password = password
            if password is not None:
                os.environ["NIX_BINARY_CACHE_PASSWORD"] = password
            self._auth = session.auth = auth
            self._session = session
            return self._session
        elif resp.status_code == 401:
            if attempts > 0:
                time.sleep(2)
                logging.info("Invalid response. Retrying...")
                return self._connect(first_time=False, attempts=attempts-1)
            elif sys.stdin.isatty():
                # Authorization failed. Give the user a chance to set new auth.
                msg = "\033[31mAuthorization failed!\033[0m\n" \
                      if not first_time else ""
                msg += "Please enter \033[1musername\033[0m"
                msg += " for {}".format(self._endpoint) if first_time else ""
                if self._username is not None:
                    msg += " (default '{}'): ".format(self._username)
                else:
                    msg += ": "
                try:
                    username = six.moves.input(msg).strip()
                    if username != "":
                        self._username = username
                    os.environ.pop("NIX_BINARY_CACHE_PASSWORD", None)
                    self._password = None
                except (KeyboardInterrupt, EOFError):
                    logging.info("\nBye!")
                    sys.exit()
                return self._connect(first_time=False)
            else:
                raise CouldNotConnect(self._endpoint, resp.status_code,
                                      resp.content)
        else:
            raise CouldNotConnect(self._endpoint, resp.status_code,
                                  resp.content)

    def send_object(self, path, remaining_objects=None, is_nar=False):
        """Send a store object to a nix server.

        :param path: The path to the store object to send.
        :type path: ``str``
        :param remaining_objects: Set of remaining objects to send.
        :type remaining_objects: ``NoneType`` or ``set`` of ``str``
        :param is_nar: If true, then we are sending a NAR. Don't
                       create another NAR of this one.
        :type is_nar: ``bool``

        Side effects:
        * Adds 0 or 1 paths to `self._objects_on_server`.
        """
        # Check if the object is already on the server; if so we can stop.
        if path in self._objects_on_server:
            return
        # First send all of the object's references. Skip self-references.
        for ref in self.get_references(path):
            self.send_object(ref, remaining_objects=remaining_objects)

        # If we're sending the NAR, send it *before* we send the
        # object; this will mean that whenever the server is asked for
        # a package, it will be able to answer quickly.
        if self._send_nars is True:
            self.send_nar(path)

        # Now we can send the object itself. Generate a dump of the
        # file and send it to the import url. For now we're not using
        # streaming because it's not entirely clear that this is
        # possible with current requests, or indeed possible in
        # general without knowing the file size.
        export = check_output(nix_cmd("nix-store", ["--export", path]))
        # For large files, show progress when compressing
        if len(export) > 1000000:
            logging.info("Compressing {}".format(basename(path)))
            cmd = "pv -ptef -s {} | gzip".format(len(export))
            proc = Popen(cmd, shell=True, stdin=PIPE, stdout=PIPE)
            data = proc.communicate(input=export)[0]
        else:
            data = gzip.compress(export)
        url = "{}/import-path".format(self._endpoint)
        headers = {"Content-Type": "application/x-gzip"}
        try:
            msg = "Sending {}".format(basename(path))
            if remaining_objects is not None:
                msg += " ({} remaining)".format(len(remaining_objects))
            logging.info(msg)
            response = self._request(url, method="post", data=data,
                                     headers=headers)
        except requests.exceptions.HTTPError as err:
            try:
                msg = json.loads(decode_str(response.content))["message"]
            except (ValueError, KeyError):
                msg = response.content
            logging.error("{} returned error on path {}: {}"
                          .format(self._endpoint, basename(path), msg))
            raise

        # Register that the store path has been sent.
        self._objects_on_server.add(path)
        # Remove the path if it is still in the set.
        if remaining_objects is not None and path in remaining_objects:
            remaining_objects.remove(path)

    def send_nar(self, store_path):
        """Send a NAR (nix-archive) of a given store path.

        Differences from send_object:
        * Hit the upload-nar route instead of import-path
        * NARs don't have references, so no need to recur on those
        * NARs are already compressed, so no need to gzip them
        """
        nar_dir = NarInfo.get_nar_dir(store_path, self._compression_type)
        if nar_dir in self._objects_on_server:
            return
        logging.info("Creating {}-compressed NAR of {}"
                     .format(self._compression_type, store_path))
        nar_path = NarInfo.build_nar(store_path, self._compression_type)
        if dirname(nar_path) != nar_dir:
            raise RuntimeError("Unexpected NAR directory: {} is not in {}"
                               .format(nar_path, nar_dir))
        export = check_output(nix_cmd("nix-store", ["--export", nar_dir]))
        url = "{}/upload-nar/{}/{}".format(self._endpoint,
                                           self._compression_type,
                                           basename(store_path))
        try:
            logging.info("Sending NAR of {} ({})"
                         .format(basename(store_path), basename(nar_path)))
            response = self._request(url, method="post", data=export)
            self._objects_on_server.add(nar_dir)
        except requests.exceptions.HTTPError as err:
            if err.response.status_code != 404:
                raise
            logging.warn("Endpoint {} doesn't support NAR uploads, turning "
                         "this option off".format(self._endpoint))
            self._send_nars = False
        finally:
            # We can remove a sent NAR because it takes up unnecessary
            # space. However, we ignore errors on the off-chance that
            # there are still objects which refer to it.
            call(nix_cmd("nix-store", ["--delete", nar_path]),
                 stderr=PIPE, stdout=PIPE)

    def send_objects(self, paths):
        """Checks for which paths need to be sent, and sends those.

        :param paths: Store paths to be sent.
        :type paths: ``list`` of ``str``
        """
        to_send = self.query_path_closures(paths)
        if self._send_nars is True and len(to_send) > 0:
            logging.info("Getting paths of NARs...")
            nar_paths = set()
            for i, path in enumerate(to_send):
                if sys.stderr.isatty() and (i % 10 == 0
                                            or i == len(to_send) - 1):
                    sys.stderr.write("{}/{}              \r"
                                     .format(i + 1, len(to_send)))
                    sys.stderr.flush()
                nar_paths.add(NarInfo.get_nar_dir(path, self._compression_type))
            if sys.stderr.isatty():
                sys.stderr.write("\n")
            logging.info("Checking for what NARs the server has...")
            query = self.query_paths(nar_paths)
            on_server, not_on_server = 0, 0
            for nar_path, is_on_server in query.items():
                if is_on_server is True:
                    # It's already on the server; we don't need to create a nar
                    self._objects_on_server.add(nar_path)
                    on_server += 1
                else:
                    not_on_server += 1
            logging.info("{} NARs are on the server, and {} are not"
                         .format(on_server, not_on_server))

        num_to_send = len(to_send)
        if num_to_send == 1:
            logging.info("1 path will be sent to {}".format(self._endpoint))
        elif num_to_send > 1:
            logging.info("{} paths will be sent to {}"
                         .format(num_to_send, self._endpoint))
        else:
            logging.info("No paths need to be sent. {} is up-to-date."
                         .format(self._endpoint))
        if self._dry_run is False:
            while len(to_send) > 0:
                self.send_object(to_send.pop(), to_send)
            if num_to_send > 0:
                logging.info("Sent {} paths to {}"
                             .format(num_to_send, self._endpoint))
        elif num_to_send <= SHOW_PATHS_LIMIT:
            for path in to_send:
                logging.info(basename(path))

    def _have_fetched(self, path):
        """Checks if we've fetched a given path, or if it exists on disk.

        :param path: The path to the store object to check.
        :type path: ``str``

        :return: Whether we've fetched the path.
        :rtype: ``bool``

        Side effects:
        * Adds 0 or 1 paths to `self._paths_fetched`.
        """
        if path in self._paths_fetched:
            return True
        elif exists(path):
            self._paths_fetched.add(path)
            return True
        else:
            return False

    def _compute_fetch_order(self, paths):
        """Given a list of paths, compute an order to fetch them in.

        The returned order will respect the dependency tree; no child
        will appear before its parent in the list. In addition, the
        returned list may be larger as some dependencies of input
        paths might not be in the original list.

        :param paths: A list of store paths.
        :type paths: ``list`` of ``str``

        :return: A list of paths in dependency-first order.
        :rtype: ``list`` of ``str``
        """
        # Start by seeing if the server supports the
        # compute-fetch-order route. If it does, we can just use that
        # and save a lot of effort and network traffic.
        try:
            url = self._endpoint + "/compute-fetch-order"
            response = self._connect().get(url, data="\n".join(paths))
            response.raise_for_status()
            pairs = json.loads(decode_str(gzip.decompress(response.content)))
            # Server also returns the references for everything in the
            # list. We can store those in our cache.
            order = []
            for item in pairs:
                path, refs = item[0], item[1]
                self._reference_cache.record_references(path, refs)
                order.append(path)
            return order
        except (requests.HTTPError, AssertionError) as err:
            logging.info("Server doesn't support compute-fetch-order "
                         "route. Have to do it ourselves...")
        order = []
        order_set = set()
        def _order(path):
            if path not in order_set:
                for ref in self.get_references(path, query_server=True):
                    _order(ref)
                order.append(path)
                order_set.add(path)
        logging.debug("Computing a fetch order for {}"
                      .format(tell_size(paths, "path")))
        for path in paths:
            _order(path)
        logging.debug("Finished computing fetch order.")
        return order

    def _fetch_unordered_paths(self, paths_to_fetch):
        """Fetch paths which are not ordered and might not be the full closure.

        :param paths_to_fetch: List of paths desired to be fetched.
        :type  paths_to_fetch: ``list`` of ``str``
        """
        if self._use_batch_fetching is True:
            try:
                self._fetch_batch(paths_to_fetch)
                return
            except OperationNotSupported:
                logging.info("Batch fetching not supported by {}. "
                             "falling back to individual fetches."
                             .format(self._endpoint))
            self._use_batch_fetching = False
            self._fetch_unordered_paths(paths_to_fetch)
        else:
            # Figure out the order to fetch them in.
            logging.info("Computing fetch order...")
            fetch_order = self._compute_fetch_order(paths_to_fetch)
            # Perform the fetches.
            self._fetch_ordered_paths(fetch_order)

    def _fetch_ordered_paths(self, store_paths):
        """Given an ordered list of paths, fetch all from a cache."""
        logging.info("Beginning fetches. Total of {} to fetch."
                     .format(tell_size(store_paths, "store object")))
        start_time = datetime.now()
        try:
            for path in store_paths:
                self._start_fetching(path)
            for i, path in enumerate(store_paths):
                logging.info("{}/{} ({})"
                             .format(i + 1, len(store_paths), basename(path)))
                self._finish_fetching(path)
            seconds = (datetime.now() - start_time).seconds
            logging.info("Finished fetching {}, took {}"
                         .format(tell_size(store_paths, "path"),
                                 format_seconds(seconds)))
        except:
            self._cancelled = True
            logging.error("Received exception. Cancelling running fetches...")
            try:
                with self._fetch_lock:
                    for path, future in self._fetch_futures.items():
                        if future.running():
                            logging.info("Cancelling fetch of", path)
                            future.cancel()
                    logging.info("Done cancelling futures")
            finally:
                raise

    def _request(self, url, method="get", **kwargs):
        """Make a request, with retry logic."""
        attempt = 1
        while True:
            logging.debug("Requesting to url '{}', method '{}', attempt {}"
                          .format(url, method, attempt))
            try:
                response = getattr(self._connect(), method)(url, **kwargs)
                response.raise_for_status()
                return response
            except requests.HTTPError as err:
                if err.response.status_code < 500 or \
                       (self._max_attempts is not None and
                        attempt >= self._max_attempts):
                    raise
                else:
                    logging.warn("Received an error response ({}) from the "
                                 "server. Retrying (attempt {} out of {})"
                                 .format(err, attempt, self._max_attempts))
                    attempt += 1
            except requests.ConnectionError as cerr:
                logging.warn("Encountered connection error {}. Reinitializing "
                             "connection".format(cerr))
                self._session = None

    def _fetch_batch(self, paths):
        """Fetch multiple paths in a batch request.

        First initializes a batch fetch session with the server. Then
        repeatedly makes request for a batch fetch tarball, until all
        paths have been fetched.
        """
        # Initialize a session
        logging.info("Initializing a batch fetching session")
        url = urljoin(self._endpoint, "init-batch-fetch")
        data = json.dumps({"paths": paths})
        headers = {"Content-Type": "application/json"}
        try:
            response = self._request(url, method="post", data=data,
                                     headers=headers)
        except requests.HTTPError as err:
            msg = "No support for batch fetching"
            raise OperationNotSupported(msg) from None
        token = response.json()["token"]
        num_total_paths = response.json()["num_total_paths"]

        logging.info("Batch-fetching {} total paths".format(num_total_paths))
        # Fetch paths until there are none left to fetch
        while self._fetch_single_batch(token) > 0:
            pass

        logging.info("Finished batch fetch.")

    def _fetch_single_batch(self, token):
        """Unpack a batch fetch tarball and import files into the nix store.

        Each response from the server should be a tarball containing
        an info file and any number of compressed NARs. The info file
        is called 'info.json' and is a JSON dictionary containing:

          * import_ordering: What order to import the NARs in.
          * nar_mapping: Mapping from nar path -> narinfo dictionary.
          * paths_remaining: How many paths are remaining to be fetched.

        This function will extract the info and use the contained
        ordering to extract each nar file and import it into the nix
        store.

        :return: The number of imported and remaining paths.
        :rtype: ``dict``, "remaining" and "imported" keys mapping to ``int``
        """
        fetch_url = urljoin(self._endpoint, "batch-fetch/" + token)
        response = self._request(fetch_url)
        bio = BytesIO(response.content)
        tar = tarfile.open(fileobj=bio, mode="r")
        member_map = {m.name: m for m in tar.getmembers()}
        if "info.json" not in member_map:
            raise ValueError("No info.json included in batch response tarball")
        info_str = decode_str(tar.extractfile(member_map["info.json"]).read())
        info = json.loads(info_str)
        remaining = info["paths_remaining"]
        nar_mapping = info["nar_mapping"]
        for nar_path in info["import_ordering"]:
            narinfo = NarInfo.from_dict(nar_mapping[nar_path])
            nar_bytes = tar.extractfile(member_map[nar_path]).read()
            narinfo.import_to_store(nar_bytes)
            self._register_as_fetched(narinfo.store_path)
        logging.info("Imported {} new paths.".format(len(nar_mapping)))
        if remaining > 0:
            logging.info("{} paths remain to be fetched.".format(remaining))
        return remaining

    def _fetch_single(self, path, retries_remaining=3):
        """Fetch a single path."""
        # Return if the path has already been fetched, or already exists.
        if self._cancelled is True:
            raise RuntimeError("Cancelled (" + path + ")")
        if self._have_fetched(path):
            return
        elif retries_remaining < 0:
            logging.error("Too many retries for path {}!".format(path))
            raise ObjectNotBuilt(path)
        # First ensure that all referenced paths have been fetched.
        for ref in self.get_references(path):
            self._finish_fetching(ref)
        # Get the info of the store path.
        narinfo = self.get_narinfo(path)

        # Use the URL in the narinfo to fetch the object.
        url = "{}/{}".format(self._endpoint, narinfo.url)
        logging.debug("Requesting {} from {}..."
                     .format(basename(path), self._endpoint))
        response = self._request(url)

        imported_path = narinfo.import_to_store(response.content)
        if not is_path_in_store(imported_path):
            logging.warn("Couldn't import fetched object for " + path)
            # delete the path before retrying
            return self._fetch_single(
                path, retries_remaining=(retries_remaining - 1))
        self._register_as_fetched(path)

    def _register_as_fetched(self, path):
        """Register that a store path has been fetched."""
        self._paths_fetched.add(path)

    def _start_fetching(self, path):
        """Start a fetch thread. Syncronized so that a fetch of a
        single path will only happen once."""
        with self._fetch_lock:
            if path not in self._fetch_futures:
                future = self._fetch_pool.submit(self._fetch_single, path)
                logging.debug("Putting fetch of path {} in future {}"
                              .format(path, future))
                self._fetch_futures[path] = future
                return future
            else:
                return self._fetch_futures[path]

    def _finish_fetching(self, path):
        """Given a path, wait until that path's fetch has finished. It
        must already have been started."""
        if self._cancelled is True:
            return
        with self._fetch_lock:
            if path not in self._fetch_futures:
                raise RuntimeError("Fetch of path {} has not been started."
                                   .format(path))
            future = self._fetch_futures[path]
        # Now that we have the future, wait for it to finish before returning.
        future.result()

    def watch_store(self, ignore=None, no_ignore=None, ignore_drvs=True,
                    ignore_tarballs=True):
        """Watch the nix store's timestamp and sync whenever it changes.

        :param ignore: A list of regexes of objects to ignore.
        :type ignore: ``NoneType`` or ``list`` of (``str`` or ``regex``)
        :param no_ignore: A list of regexes of objects to include, even
                          if they would otherwise be ignored.
        :type no_ignore: ``NoneType`` or ``list`` of (``str`` or ``regex``)
        :param ignore_drvs: If true, ignore any nix derivation files.
        :type ignore_drvs: ``bool``
        :param ignore_tarballs: If true, ignore files which appear to
                                be tarballs or zip files.
        :type ignore_tarballs: ``bool``
        """
        prev_stamp = None
        num_syncs = 0
        try:
            while True:
                # Parse the timestamp of the nix store into a datetime
                stamp = datetime.fromtimestamp(getmtime(NIX_STORE_PATH))
                # If it's changed since last time, run a sync.
                if stamp == prev_stamp:
                    logging.debug("Store hasn't updated since last check ({})"
                                  .format(stamp.strftime("%H:%M:%S")))
                    time.sleep(1)
                    continue
                else:
                    logging.info("Store was modified at {}, syncing"
                                 .format(stamp.strftime("%H:%M:%S")))
                try:
                    self.sync_store(ignore=ignore, no_ignore=no_ignore,
                                    ignore_drvs=ignore_drvs,
                                    ignore_tarballs=ignore_tarballs)
                    prev_stamp = stamp
                    num_syncs += 1
                except requests.exceptions.HTTPError as err:
                    # Don't fail the daemon due to a failed sync.
                    pass
        except KeyboardInterrupt:
            exit("Successfully syncronized with {} {} times."
                 .format(self._endpoint, num_syncs))

    def sync_store(self, ignore=None, no_ignore=None, ignore_drvs=True,
                   ignore_tarballs=True):
        """Syncronize the local nix store to the endpoint.

        Reads all of the known paths in the nix SQLite database which
        don't match the ignore patterns, and passes them into
        :py:meth:`send_objects`.

        :param ignore: A list of regexes of objects to ignore.
        :type ignore: ``NoneType`` or ``list`` of (``str`` or ``regex``)
        :param no_ignore: A list of regexes of objects to include, even
                          if they would otherwise be ignored.
        :type no_ignore: ``NoneType`` or ``list`` of (``str`` or ``regex``)
        :param ignore_drvs: If true, ignore any nix derivation files.
        :type ignore_drvs: ``bool``
        :param ignore_tarballs: If true, ignore files which appear to
                                be tarballs or zip files.
        :type ignore_tarballs: ``bool``
        """
        ignore = [re.compile(r) for r in (ignore or [])]
        no_ignore = [re.compile(r) for r in (no_ignore or [])]
        paths = []
        ignored_due_to_regex = set()
        ignored_derivations = set()
        ignored_tarballs = set()
        with self._db_con as con:
            query = con.execute("SELECT path FROM ValidPaths")
            for result in query.fetchall():
                path = result[0]
                if any(ig.match(path) for ig in ignore):
                    if any(no_ig.match(path) for no_ig in no_ignore):
                        logging.debug("Path {} would be ignored, but matches "
                                      "a no-ignore regex".format(path))
                    else:
                        logging.debug("Path {} matches an ignore regex"
                                      .format(path))
                        ignored_due_to_regex.add(path)
                        continue
                if ignore_drvs is True and path.endswith(".drv"):
                    if any(no_ig.match(path) for no_ig in no_ignore):
                        logging.debug("Path {} is a derivation, but matches "
                                      "a no-ignore regex".format(path))
                    else:
                        logging.debug("Path {} appears to be a derivation"
                                      .format(path))
                        ignored_derivations.add(path)
                        continue
                if ignore_tarballs is True:
                    try:
                        mimetype = decode_str(magic.from_file(path, mime=True))
                        if mimetype in TARBALL_MIMETYPES:
                            if any(no_ig.match(path) for no_ig in no_ignore):
                                logging.debug("Path {} is a tarball, but "
                                              "matches a no-ignore regex"
                                              .format(path))
                            else:
                                logging.debug("Path {} appears to be a tarball"
                                              .format(path))
                                ignored_tarballs.add(path)
                                continue
                    except Exception:
                        pass
                paths.append(path)
        logging.info("Found {} paths in the store.".format(len(paths)))
        if len(ignored_due_to_regex) > 0:
            logging.info("{} skipped due to matching an ignore regex"
                         .format(tell_size(ignored_due_to_regex, "path")))
        if len(ignored_derivations) > 0:
            logging.info("{} skipped because --ignore-drvs"
                         .format(tell_size(ignored_derivations, "derivation")))
        if len(ignored_tarballs) > 0:
            logging.info("{} skipped because --ignore-tarballs"
                         .format(tell_size(ignored_tarballs, "tarball")))
        self.send_objects(paths)

    def build_fetch(self, nix_file, attributes, show_trace=True, **kwargs):
        """Given a nix file, instantiate the given attributes within the file,
        query the server for which files can be fetched, and then
        build/fetch everything.

        :return: A dictionary mapping derivations to outputs that were built.
        :rtype: ``dict``
        """
        logging.info("Instantiating attribute{} {} from path {}"
                     .format("s" if len(attributes) > 1 else "",
                             ", ".join(attributes), nix_file))
        deriv_paths = instantiate(nix_file, attributes=attributes,
                                  show_trace=show_trace)
        logging.info("Building {}"
                     .format(tell_size(deriv_paths, "top-level derivation")))
        return self.build_derivations(deriv_paths, **kwargs)

    def build_derivations(self, deriv_paths, verbose=False, keep_going=True,
                          create_links=False, use_deriv_name=True):
        """Given one or more derivation paths, build the derivations."""
        if len(deriv_paths) == 0:
            logging.info("No paths given, nothing to build.")
            return
        derivs_to_outputs = parse_deriv_paths(deriv_paths)
        need_to_build, need_to_fetch = self.preview_build(deriv_paths)
        if self._dry_run is True:
            self.print_preview(need_to_build, need_to_fetch, verbose)
            return
        # Build the list of paths to fetch from the remote store.
        paths_to_fetch = []
        for deriv, outputs in need_to_fetch.items():
            for output in outputs:
                paths_to_fetch.append(deriv.output_path(output))
        if len(paths_to_fetch) > 0:
            self._fetch_unordered_paths(paths_to_fetch)
            self._verify(need_to_fetch)
        # Build up the command for nix store to build the remaining paths.
        if len(need_to_build) > 0:
            args = ["--max-jobs", str(self._max_jobs), "--no-gc-warning",
                    "--realise"]
            args.extend(d.path for d in need_to_build)
            if keep_going is True:
                args.append("--keep-going")
            logging.info("Building {} locally"
                         .format(tell_size(need_to_build, "derivation")))
            cmd = nix_cmd("nix-store", args)
            build_start_time = datetime.now()
            try:
                strip_output(cmd).split()
            except CalledProcessError as err:
                self._handle_build_failure(need_to_build)
            finally:
                build_seconds = (datetime.now() - build_start_time).seconds
                logging.info("Building derivations locally took {}"
                             .format(format_seconds(build_seconds)))

        else:
            logging.info("No derivations needed to be built locally")
        if create_links is True:
            self._create_symlinks(derivs_to_outputs, use_deriv_name)
        return derivs_to_outputs

    def _handle_build_failure(self, derivs_to_outputs):
        """In a failure situation, report which derivations succeeded and
        which failed.
        """
        # TODO: report exactly which derivations succeeded/failed.
        failed_to_build = set()
        for deriv, outputs in derivs_to_outputs.items():
            if any(not exists(p) for p in deriv.output_paths(outputs)):
                # Then this derivation wasn't built. However, it might
                # not have failed to build; it might have been an
                # upstream derivation that failed. To check this, see
                # if all of its derivation inputs exist.
                if all(exists(p) for p in deriv.input_derivation_paths):
                    logging.debug("All inputs of {} exist, so it must have "
                                  "failed to build.".format(deriv))
                    failed_to_build.add(deriv)

        logging.error("These derivations were attempted to build, but failed:")
        for failed in failed_to_build:
            logging.error("  " + failed.path)
        raise NixBuildError()

    def _create_symlinks(self, derivs_to_outputs, use_deriv_name):
        """Create symlinks to all built derivations.

        :param derivs_to_outputs: Maps derivations to sets of output names.
        :type derivs_to_outputs:
            ``dict`` of ``Derivation`` to ``set`` of ``str``
        :param use_deriv_name: If true, the symlink names will be
                               generated from derivation names.
                               Otherwise, `result` will be used.
        :type use_deriv_name: ``bool``
        """
        count = 0
        for deriv, outputs in derivs_to_outputs.items():
            for output in outputs:
                path = deriv.output_path(output)
                if use_deriv_name:
                    link_path = deriv.link_path(output)
                else:
                    link_path = join(os.getcwd(), "result")
                    if output != "out":
                        link_path += "-" + output
                    if count > 0:
                        link_path += "-" + str(count)
                args = ["--realise", path, "--add-root", link_path,
                        "--indirect"]
                check_output(nix_cmd("nix-store", args))
                count += 1

    def preview_build(self, paths):
        """Given some derivation paths, generate two sets:

        * Set of derivations which need to be built from scratch
        * Set of derivations which can be fetched from a binary cache

        Of course, the second set will be empty if no binary cache is given.
        """
        if isinstance(paths, dict):
            derivs_outs = paths
        else:
            derivs_outs = parse_deriv_paths(paths)
        existing = {}
        # Run the first time with no on_server argument.
        needed, need_fetch = needed_to_build_multi(derivs_outs, existing=existing)
        if len(needed) > 0:
            logging.info("{} were not in the local nix store. Querying {} to "
                         "see which paths it has..."
                         .format(tell_size(needed, "needed object"),
                                 self._endpoint))
            on_server = {}
            # Query the server for missing paths. Start by trying a
            # multi-query because it's faster; if the server doesn't
            # implement that behavior then try individual queries.
            paths_to_ask = []
            # Make a dictionary mapping paths back to the
            # derivations/outputs they came from.
            path_mapping = {}
            for deriv, outs in needed.items():
                for out in outs:
                    path = deriv.output_path(out)
                    paths_to_ask.append(path)
                    path_mapping[path] = (deriv, out)
            if self._endpoint is None:
                # If there's no endpoint, then it's the same as the
                # server not having any paths.
                query_result = {p: False for p in paths_to_ask}
            else:
                query_result = self.query_paths(paths_to_ask)
            for path, is_on_server in query_result.items():
                if is_on_server is not True:
                    continue
                deriv, out_name = path_mapping[path]
                # First, remove these from the `needed` set, because
                # we can fetch them from the server.
                needed[deriv].remove(out_name)
                if len(needed[deriv]) == 0:
                    del needed[deriv]
                # Then add them to the `on_server` set.
                if deriv not in on_server:
                    on_server[deriv] = set()
                on_server[deriv].add(out_name)
            if len(on_server) > 0:
                # Run the check again, this time using the information
                # collected from the server.
                needed, need_fetch = needed_to_build_multi(derivs_outs,
                                                           on_server=on_server,
                                                           existing=existing)
        return needed, need_fetch

    def print_preview(self, need_to_build, need_to_fetch, verbose=False):
        """Print the result of a `preview_build` operation."""
        if len(need_to_build) == 0 and len(need_to_fetch) == 0:
            logging.info("All paths have already been built.")
        if len(need_to_build) > 0:
            verbose_ = verbose or len(need_to_build) < SHOW_PATHS_LIMIT
            msg = (("{} will be built" + (":" if verbose_ else "."))
                   .format(tell_size(need_to_build, "derivation")))
            if verbose_:
                for deriv in need_to_build:
                    msg += "\n  " + deriv.path
            logging.info(msg)
        if len(need_to_fetch) > 0:
            verbose_ = verbose or len(need_to_fetch) < SHOW_PATHS_LIMIT
            msg = (("{} will be fetched from {}" + (":" if verbose_ else "."))
                   .format(tell_size(need_to_fetch, "path"), self._endpoint))
            if verbose_:
                for deriv, outs in need_to_fetch.items():
                    for out in outs:
                        msg += "\n  " + deriv.output_path(out)
            logging.info(msg)

def _get_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(prog="nix-client")
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(title="Command", dest="command")
    subparsers.required = True
    # 'send' command used for sending particular paths.
    send = subparsers.add_parser("send", help="Send specific store objects.")
    send.add_argument("paths", nargs="+", help="Store paths to send.")
    # 'sync' command used for syncronizing an entire nix store.
    sync = subparsers.add_parser("sync", help="Send all store objects.")
    daemon = subparsers.add_parser("daemon",
                                   help="Run as daemon, periodically "
                                        "syncing store.")
    for p in (sync, daemon):
        p.add_argument("--no-ignore-drvs", action="store_false",
                       dest="ignore_drvs",
                       help="Don't ignore .drv files when syncing")
        p.add_argument("--no-ignore-tarballs", action="store_false",
                       dest="ignore_tarballs",
                       help="Don't ignore tarball files when syncing")
        p.add_argument("--ignore", nargs="*", default=[],
                       help="Regexes of store paths to ignore.")
        p.add_argument("--no-ignore", nargs="*", default=[],
                       help="Don't ignore these, even if they would "
                            "normally be ignored.")
        p.set_defaults(ignore_drvs=True, ignore_tarballs=True)

    fetch = subparsers.add_parser("fetch",
                                   help="Fetch objects from a nix server.")
    fetch.add_argument("paths", nargs="+", help="Paths to fetch.")
    build = subparsers.add_parser("build",
        help="Build a nix expression, using the server as a binary cache.")
    build.add_argument("-P", "--path", default=os.getcwd(),
                       help="Base path to evaluate.")
    build.add_argument("attributes", nargs="*",
                       help="Expressions to evaluate.")
    build.add_argument("--no-trace", action="store_false", dest="show_trace",
                       help="Hide stack trace on instantiation error.")
    build.set_defaults(show_trace=True)
    build_derivations = subparsers.add_parser("build-derivations",
        help="Build one or more derivations.")
    build_derivations.add_argument("derivations", nargs="*",
                                   help="Paths of derivation files")
    build_derivations.add_argument("-f", "--from-file",
                                   help="Read paths from the given file")
    for p in (build, build_derivations):
        p.add_argument("-v", "--verbose", action="store_true", default=False,
                       help="Show verbose output.")
        p.add_argument("-S", "--stop-on-failure", action="store_false",
                       dest="keep_going",
                       help="Stop all builders if any builder fails.")
        p.add_argument("--hide-paths", action="store_false",
                       dest="print_paths",
                       help="Don't print built paths to stdout")
        p.add_argument("-C", "--create-links", action="store_true",
                       default=False, help="Create symlinks to built objects.")
        p.add_argument("-g", "--generic-link-name", action="store_true",
                       default=False,
                       help="Use generic `result` name for symlinks.")
        p.add_argument("-1", "--one", action="store_true", default=False,
                       help="Alias for '--max-jobs=1 --stop-on-failure'")
        p.set_defaults(show_trace=True, keep_going=True, print_paths=True)

    for subparser in (send, sync, daemon, fetch, build, build_derivations):
        subparser.add_argument("--batch",
                               help="Use batch fetching when available.")
        subparser.add_argument("--no-batch", action="store_false",
                               dest="batch", help="Disable batch fetching")
        subparser.set_defaults(batch=os.getenv("NO_BATCH", "") == "")

        subparser.add_argument("-e", "--endpoint",
                               default=os.environ.get("NIX_REPO_HTTP"),
                               help="Endpoint of nix server to send to.")
        subparser.set_defaults(log_level=os.getenv("LOG_LEVEL", "INFO"))
        subparser.add_argument("--max-attempts", type=int, default=3,
                               help="Maximum attempts to make for requests.")
        subparser.add_argument("--no-max-attempts", action="store_const",
                               const=None, dest="max_attempts",
                               help="No maximum on number of attempts.")
        for level in ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"):
            subparser.add_argument("--" + level.lower(), dest="log_level",
                                   action="store_const", const=level)
        subparser.add_argument("-u", "--username",
            default=os.environ.get("NIX_BINARY_CACHE_USERNAME"),
            help="User to authenticate to the cache as.")
        subparser.add_argument("--max-jobs", type=int, default=cpu_count(),
                               help="For concurrency, max workers.")
        subparser.add_argument("-D", "--dry-run", action="store_true",
                               default=False,
                               help="If true, reports which paths would "
                                    "be sent/fetched/built.")
        subparser.add_argument("--send-nars", action="store_true",
                               default=os.getenv("SEND_NARS", "") != "",
                               help="Also send NARs for the objects.")
        for t in sorted(set(COMPRESSION_TYPES) |
                        set(COMPRESSION_TYPE_ALIASES)):
            subparser.add_argument("--" + t, action="store_const", const=t,
                                   dest="compression_type",
                                   help="Use {} compression for sent NARs."
                                        .format(resolve_compression_type(t)))
            subparser.set_defaults(
                compression_type=os.getenv("COMPRESSION_TYPE", "xz"))

    return parser.parse_args()

def main():
    """Main entry point."""
    args = _get_args()
    if not args.endpoint: # treat empty strings as None
        if args.command in ("send", "sync", "daemon", "fetch"):
            exit("Operation '{}' requires an endpoint to be specified."
                 .format(args.command))
        args.endpoint = None
    if args.endpoint is not None and \
       ENDPOINT_REGEX.match(args.endpoint) is None:
        exit("Invalid endpoint: '{}' does not match '{}'."
             .format(args.endpoint, ENDPOINT_REGEX.pattern))
    log_level = getattr(logging, args.log_level.upper())
    logging.basicConfig(level=log_level, format="%(message)s")
    # Hide noisy logging of some external libs
    for name in ("requests", "urllib", "urllib2", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)
    max_jobs = 1 if getattr(args, "one", False) else args.max_jobs
    client = NixCacheClient(endpoint=args.endpoint, dry_run=args.dry_run,
                            username=args.username, max_jobs=max_jobs,
                            compression_type=args.compression_type,
                            send_nars=args.send_nars,
                            use_batch_fetching=args.batch,
                            max_attempts=args.max_attempts)
    try:
        if args.command == "send":
            client.send_objects(args.paths)
        elif args.command == "sync":
            client.sync_store(ignore=args.ignore, no_ignore=args.no_ignore,
                              ignore_drvs=args.ignore_drvs,
                              ignore_tarballs=args.ignore_tarballs)
        elif args.command == "daemon":
            client.watch_store(ignore=args.ignore, no_ignore=args.no_ignore,
                               ignore_drvs=args.ignore_drvs,
                               ignore_tarballs=args.ignore_tarballs)
        elif args.command == "fetch":
            client._fetch_unordered_paths(args.paths)
        elif args.command == "build":
            keep_going = False if args.one else args.keep_going
            result_derivs = client.build_fetch(
                nix_file=args.path, attributes=args.attributes,
                verbose=args.verbose, show_trace=args.show_trace,
                keep_going=keep_going, create_links=args.create_links,
                use_deriv_name=not args.generic_link_name)
            if args.dry_run is False and args.print_paths is True:
                for deriv, outputs in result_derivs.items():
                    for output in outputs:
                        print(deriv.output_path(output))
        elif args.command == "build-derivations":
            keep_going = False if args.one else args.keep_going
            deriv_paths = args.derivations
            if args.from_file is not None:
                with open(args.from_file) as f:
                    deriv_paths.extend(f.read().split())
            result_derivs = client.build_derivations(
                deriv_paths=deriv_paths,
                verbose=args.verbose, keep_going=keep_going,
                create_links=args.create_links,
                use_deriv_name=not args.generic_link_name)
            if args.dry_run is False and args.print_paths is True:
                for deriv, outputs in result_derivs.items():
                    for output in outputs:
                        print(deriv.output_path(output))
        else:
            exit("Unknown command '{}'".format(args.command))
    except CliError as err:
        err.exit()
