# Copyright 2019 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Build manager."""

from collections import namedtuple
import contextlib
import datetime
import os
import re
import shutil
import subprocess
import time
from typing import Optional

from clusterfuzz._internal.base import errors
from clusterfuzz._internal.base import utils
from clusterfuzz._internal.build_management import build_archive
from clusterfuzz._internal.build_management import overrides
from clusterfuzz._internal.build_management import revisions
from clusterfuzz._internal.config import local_config
from clusterfuzz._internal.datastore import data_types
from clusterfuzz._internal.datastore import ndb_utils
from clusterfuzz._internal.fuzzing import fuzzer_selection
from clusterfuzz._internal.google_cloud_utils import blobs
from clusterfuzz._internal.google_cloud_utils import storage
from clusterfuzz._internal.metrics import logs
from clusterfuzz._internal.metrics import monitoring_metrics
from clusterfuzz._internal.platforms import android
from clusterfuzz._internal.system import archive
from clusterfuzz._internal.system import environment
from clusterfuzz._internal.system import shell

# The default environment variables for specifying build bucket paths.
DEFAULT_BUILD_BUCKET_PATH_ENV_VARS = (
    'RELEASE_BUILD_BUCKET_PATH',
    'SYM_RELEASE_BUILD_BUCKET_PATH',
    'SYM_DEBUG_BUILD_BUCKET_PATH',
)

# File name for storing current build revision.
REVISION_FILE_NAME = 'REVISION'

# Various build type mapping strings.
BUILD_TYPE_SUBSTRINGS = [
    '-beta', '-stable', '-debug', '-release', '-symbolized', '-extended_stable'
]

# Build eviction constants.
MAX_EVICTED_BUILDS = 100
MIN_FREE_DISK_SPACE_CHROMIUM = 10 * 1024 * 1024 * 1024  # 10 GB
MIN_FREE_DISK_SPACE_DEFAULT = 5 * 1024 * 1024 * 1024  # 5 GB
TIMESTAMP_FILE = '.timestamp'

# Indicates if this is a partial build (due to selected files copied from fuzz
# target).
PARTIAL_BUILD_FILE = '.partial_build'

# Time for unpacking a build beyond which an error should be logged.
UNPACK_TIME_LIMIT = 60 * 20

PATCHELF_SIZE_LIMIT = 1.5 * 1024 * 1024 * 1024  # 1.5 GiB

TARGETS_LIST_FILENAME = 'targets.list'

BuildUrls = namedtuple('BuildUrls', ['bucket_path', 'urls_list'])


class BuildManagerError(Exception):
  """Build manager exceptions."""


def _base_build_dir(bucket_path):
  """Get the base directory for a build."""
  job_name = environment.get_value('JOB_NAME')
  return _get_build_directory(bucket_path, job_name)


def _make_space(requested_size, current_build_dir=None):
  """Try to make the requested number of bytes available by deleting builds."""
  if utils.is_chromium():
    min_free_disk_space = MIN_FREE_DISK_SPACE_CHROMIUM
  else:
    min_free_disk_space = MIN_FREE_DISK_SPACE_DEFAULT

  builds_directory = environment.get_value('BUILDS_DIR')

  error_message = 'Need at least %d GB of free disk space.' % (
      (min_free_disk_space + requested_size) // 1024**3)
  for _ in range(MAX_EVICTED_BUILDS):
    free_disk_space = shell.get_free_disk_space(builds_directory)
    if free_disk_space is None:
      # Can't determine free disk space, bail out.
      return False

    if requested_size + min_free_disk_space < free_disk_space:
      return True

    if not _evict_build(current_build_dir):
      logs.error(error_message)
      return False

  free_disk_space = shell.get_free_disk_space(builds_directory)
  result = requested_size + min_free_disk_space < free_disk_space
  if not result:
    logs.error(error_message)
  return result


def _evict_build(current_build_dir):
  """Remove the least recently used build to make room."""
  builds_directory = environment.get_value('BUILDS_DIR')
  least_recently_used = None
  least_recently_used_timestamp = None

  for build_directory in os.listdir(builds_directory):
    absolute_build_directory = os.path.abspath(
        os.path.join(builds_directory, build_directory))
    if not os.path.isdir(absolute_build_directory):
      continue

    if os.path.commonpath(
        [absolute_build_directory,
         os.path.abspath(current_build_dir)]) == absolute_build_directory:
      # Don't evict the build we're trying to extract. This could be a parent
      # directory of where we're currently extracting to.
      continue

    build = BaseBuild(absolute_build_directory)
    timestamp = build.last_used_time()

    if (least_recently_used_timestamp is None or
        timestamp < least_recently_used_timestamp):
      least_recently_used_timestamp = timestamp
      least_recently_used = build

  if not least_recently_used:
    return False

  logs.info(
      'Deleting build %s to save space.' % least_recently_used.base_build_dir)
  least_recently_used.delete()

  return True


def _handle_unrecoverable_error_on_windows():
  """Handle non-recoverable error on Windows. This is usually either due to disk
  corruption or processes failing to terminate using regular methods. Force a
  restart for recovery."""
  if environment.platform() != 'WINDOWS':
    return

  logs.error('Unrecoverable error, restarting machine...')
  time.sleep(60)
  utils.restart_machine()


def _remove_scheme(bucket_path):
  """Remove scheme from the bucket path."""
  if '://' not in bucket_path:
    raise BuildManagerError('Invalid bucket path: ' + bucket_path)

  return bucket_path.split('://')[1]


def _get_build_directory(bucket_path, job_name):
  """Return the build directory based on bucket path and job name."""
  builds_directory = environment.get_value('BUILDS_DIR')

  # In case we have a bucket path, we want those to share the same build
  # directory.
  if bucket_path:
    path = _remove_scheme(bucket_path).lstrip('/')
    bucket_path, file_pattern = path.rsplit('/', 1)
    bucket_path = bucket_path.replace('/', '_')

    # Remove similar build types to force them in same directory.
    file_pattern = utils.remove_sub_strings(file_pattern, BUILD_TYPE_SUBSTRINGS)

    file_pattern_hash = utils.string_hash(file_pattern)
    job_directory = f'{bucket_path}_{file_pattern_hash}'
  else:
    job_directory = job_name

  return os.path.join(builds_directory, job_directory)


def set_random_fuzz_target_for_fuzzing_if_needed(fuzz_targets, target_weights):
  """Sets a random fuzz target for fuzzing."""
  if not environment.is_engine_fuzzer_job():
    return None

  fuzz_targets = list(fuzz_targets)
  if not fuzz_targets:
    logs.error('No fuzz targets found. Unable to pick random one.')
    return None

  fuzz_target = fuzzer_selection.select_fuzz_target(fuzz_targets,
                                                    target_weights)
  logs.info(f'Picked fuzz target {fuzz_target} for fuzzing.')

  return fuzz_target


def _setup_build_directories(base_build_dir):
  """Set up build directories for a job."""
  # Create the root build directory for this job.
  shell.create_directory(base_build_dir, create_intermediates=True)

  custom_binary_directory = os.path.join(base_build_dir, 'custom')
  revision_build_directory = os.path.join(base_build_dir, 'revisions')
  sym_build_directory = os.path.join(base_build_dir, 'symbolized')
  sym_debug_build_directory = os.path.join(sym_build_directory, 'debug')
  sym_release_build_directory = os.path.join(sym_build_directory, 'release')
  build_directories = [
      custom_binary_directory, revision_build_directory, sym_build_directory,
      sym_debug_build_directory, sym_release_build_directory
  ]
  for build_directory in build_directories:
    shell.create_directory(build_directory)


def set_environment_vars(search_directories, app_path='APP_PATH',
                         env_prefix=''):
  """Set build-related environment variables (APP_PATH, APP_DIR etc) by walking
  through the build directory."""
  app_name = environment.get_value(env_prefix + 'APP_NAME')
  llvm_symbolizer_filename = environment.get_executable_filename(
      'llvm-symbolizer')
  llvm_symbolizer_path = None
  gn_args_filename = 'args.gn'
  gn_args_path = None
  platform = environment.platform()
  absolute_file_path = None
  app_directory = None
  use_default_llvm_symbolizer = environment.get_value(
      'USE_DEFAULT_LLVM_SYMBOLIZER')

  # Chromium specific folder to ignore.
  initialexe_folder_path = f'{os.path.sep}initialexe'

  logs.info('\n'.join([
      'Walking build directory to find files and set environment variables.',
      f'Environment prefix: {env_prefix!r}',
      f'App path environment variable name: {app_path!r}',
      f'App name: {app_name!r}',
      f'LLVM symbolizer file name: {llvm_symbolizer_filename!r}',
      f'Use default LLVM symbolizer: {use_default_llvm_symbolizer}',
  ]))

  def set_env_var(name, value):
    full_name = env_prefix + name
    logs.info(f'Setting environment variable: {full_name} = {value}')
    environment.set_value(full_name, value)

  for search_directory in search_directories:
    logs.info(f'Searching in directory: {search_directory}')
    for root, _, files in shell.walk(search_directory):
      # .dSYM folder contain symbol files on Mac and should
      # not be searched for application binary.
      if platform == 'MAC' and '.dSYM' in root:
        continue

      # Ignore some folders on Windows.
      if (platform == 'WINDOWS' and (initialexe_folder_path in root)):
        continue

      for filename in files:
        if not absolute_file_path and filename == app_name:
          absolute_file_path = os.path.join(root, filename)
          app_directory = os.path.dirname(absolute_file_path)
          os.chmod(absolute_file_path, 0o750)

          set_env_var(app_path, absolute_file_path)
          set_env_var('APP_DIR', app_directory)

        if not gn_args_path and filename == gn_args_filename:
          gn_args_path = os.path.join(root, gn_args_filename)
          set_env_var('GN_ARGS_PATH', gn_args_path)

        if (not llvm_symbolizer_path and
            filename == llvm_symbolizer_filename and
            not use_default_llvm_symbolizer):
          llvm_symbolizer_path = os.path.join(root, llvm_symbolizer_filename)
          set_env_var('LLVM_SYMBOLIZER_PATH', llvm_symbolizer_path)

  if app_name and not absolute_file_path:
    logs.error(f'Could not find app {app_name!r} in search directories.')


def _emit_job_build_retrieval_metric(start_time, step, build_type):
  elapsed_minutes = (time.time() - start_time) / 60
  monitoring_metrics.JOB_BUILD_RETRIEVAL_TIME.add(
      elapsed_minutes, {
          'job': os.getenv('JOB_NAME'),
          'platform': environment.platform(),
          'step': step,
          'build_type': build_type,
      })


class BaseBuild:
  """Represents a build."""

  def __init__(self, base_build_dir):
    self.base_build_dir = base_build_dir

  def last_used_time(self):
    """Return the last used time for the build."""
    timestamp_file_path = os.path.join(self.base_build_dir, TIMESTAMP_FILE)
    timestamp = utils.read_data_from_file(timestamp_file_path, eval_data=True)

    return timestamp or 0

  def delete(self):
    """Delete this build."""
    shell.remove_directory(self.base_build_dir)


class Build(BaseBuild):
  """Represent a build type at a particular revision."""

  def __init__(self,
               base_build_dir,
               revision,
               build_prefix='',
               fuzz_target=None):
    super().__init__(base_build_dir)
    self.revision = revision
    self.build_prefix = build_prefix
    self.env_prefix = build_prefix + '_' if build_prefix else ''
    # This is used by users of the class to learn the fuzz targets in the build.
    self._fuzz_targets = None
    self._unpack_everything = environment.get_value(
        'UNPACK_ALL_FUZZ_TARGETS_AND_FILES', default_value=False)
    # This is used by users of the class to instruct the class which fuzz
    # target to unpack.
    self.fuzz_target = fuzz_target
    # Every fetched build is a release one, except when SymbolizedBuild
    # explicitly downloads a debug build
    self._build_type = 'release'

  def _reset_cwd(self):
    """Reset current working directory. Needed to clean up build
    without hitting dir-in-use exception on Windows."""
    root_directory = environment.get_value('ROOT_DIR')
    os.chdir(root_directory)

  def _delete_partial_build_file(self):
    """Deletes partial build file (if present). This is needed to make sure we
    clean up build directory if the previous build was partial."""
    partial_build_file_path = os.path.join(self.build_dir, PARTIAL_BUILD_FILE)
    if os.path.exists(partial_build_file_path):
      self.delete()

  def _pre_setup(self):
    """Common pre-setup."""
    self._reset_cwd()
    shell.clear_temp_directory()

    self._delete_partial_build_file()

    if self.base_build_dir:
      _setup_build_directories(self.base_build_dir)

    environment.set_value(self.env_prefix + 'APP_REVISION', self.revision)
    environment.set_value(self.env_prefix + 'APP_PATH', '')
    environment.set_value(self.env_prefix + 'APP_PATH_DEBUG', '')

  def _patch_rpath(self, binary_path, instrumented_library_paths):
    """Patch rpaths of a binary to point to instrumented libraries"""
    rpaths = get_rpaths(binary_path)
    # Discard all RPATHs that aren't relative to build.
    rpaths = [rpath for rpath in rpaths if '$ORIGIN' in rpath]

    for additional_path in reversed(instrumented_library_paths):
      if additional_path not in rpaths:
        rpaths.insert(0, additional_path)

    set_rpaths(binary_path, rpaths)

  def _patch_rpaths(self, instrumented_library_paths):
    """Patch rpaths of builds to point to instrumented libraries."""
    if environment.is_engine_fuzzer_job():
      # Import here as this path is not available in App Engine context.
      from clusterfuzz._internal.bot.fuzzers import utils as fuzzer_utils

      for target_path in fuzzer_utils.get_fuzz_targets(self.build_dir):
        self._patch_rpath(target_path, instrumented_library_paths)
    else:
      app_path = environment.get_value('APP_PATH')
      if app_path:
        self._patch_rpath(app_path, instrumented_library_paths)

      app_path_debug = environment.get_value('APP_PATH_DEBUG')
      if app_path_debug:
        self._patch_rpath(app_path_debug, instrumented_library_paths)

  def _post_setup_success(self, update_revision=True):
    """Common post-setup."""
    if update_revision:
      self._write_revision()

    # Update timestamp to indicate when this build was last used.
    if self.base_build_dir:
      timestamp_file_path = os.path.join(self.base_build_dir, TIMESTAMP_FILE)
      utils.write_data_to_file(time.time(), timestamp_file_path)

    # Update rpaths if necessary (for e.g. instrumented libraries).
    instrumented_library_paths = environment.get_instrumented_libraries_paths()
    if instrumented_library_paths:
      self._patch_rpaths(instrumented_library_paths)

  @contextlib.contextmanager
  def _download_and_open_build_archive(self, base_build_dir: str,
                                       build_dir: str, build_url: str):
    """Downloads the build archive at `build_url` and opens it.

    Args:
        base_build_dir: the base build directory
        build_dir: the current build directory
        build_url: the build URL

    Yields:
        the build archive
    """
    # Download build archive locally.
    build_local_archive = os.path.join(build_dir, os.path.basename(build_url))

    # Make the disk space necessary for the archive available.
    archive_size = storage.get_object_size(build_url)
    if archive_size is not None and not _make_space(archive_size,
                                                    base_build_dir):
      shell.clear_data_directories()
      logs.log_fatal_and_exit(
          'Failed to make space for download. '
          'Cleared all data directories to free up space, exiting.')

    logs.info(f'Downloading build from {build_url} to {build_local_archive}.')
    try:
      start_time = time.time()
      storage.copy_file_from(build_url, build_local_archive)
      _emit_job_build_retrieval_metric(start_time, 'download', self._build_type)
    except Exception as e:
      logs.error(f'Unable to download build from {build_url}: {e}')
      raise

    try:
      with build_archive.open(build_local_archive) as build:
        yield build
    finally:
      shell.remove_file(build_local_archive)

  def _open_build_archive(self, base_build_dir: str, build_dir: str,
                          build_url: str, http_build_url: Optional[str]):
    """Gets a handle on a build archive for the current build. Depending on the
    provided parameters, this function might download the build archive into
    the build directory or directly use remote HTTP archive.

    Args:
        base_build_dir: the base build directory.
        build_dir: the current build directory.
        build_url: the build URL.
        http_build_url: the HTTP build URL.

    Raises:
        if an error occurred while accessing the file over HTTP or while
        downloading the file on disk.

    Returns:
        the build archive.
    """
    # We only want to use remote unzipping if we're not unpacking everything and
    # if the HTTP URL is compatible with remote unzipping.
    allow_unpack_over_http = environment.get_value(
        'ALLOW_UNPACK_OVER_HTTP', default_value=False)
    can_unpack_over_http = (
        allow_unpack_over_http and http_build_url and
        build_archive.unzip_over_http_compatible(http_build_url))

    if not can_unpack_over_http:
      return self._download_and_open_build_archive(base_build_dir, build_dir,
                                                   build_url)
    # We do not emmit a metric for build download time, if using http
    logs.info("Opening an archive over HTTP, skipping archive download.")
    assert http_build_url
    return build_archive.open_uri(http_build_url)

  def _unpack_build(self,
                    base_build_dir,
                    build_dir,
                    build_url,
                    http_build_url=None):
    """Unpacks a build from a build url into the build directory."""
    # Track time taken to unpack builds so that it doesn't silently regress.
    start_time = time.time()

    logs.info(f'Unpacking build from {build_url} into {build_dir}.')

    # Free up memory.
    utils.python_gc()

    # Remove the current build.
    logs.info(f'Removing build directory {build_dir}.')
    if not shell.remove_directory(build_dir, recreate=True):
      logs.error(f'Unable to clear build directory {build_dir}.')
      _handle_unrecoverable_error_on_windows()
      return False

    try:
      with self._open_build_archive(base_build_dir, build_dir, build_url,
                                    http_build_url) as build:
        unpack_start_time = time.time()
        if not self._unpack_everything:
          # We will never unpack the full build so we need to get the targets
          # from the build archive.
          list_fuzz_target_start_time = time.time()
          self._fuzz_targets = list(build.list_fuzz_targets())
          _emit_job_build_retrieval_metric(list_fuzz_target_start_time,
                                           'list_fuzz_targets',
                                           self._build_type)
          # We only want to unpack a single fuzz target if unpack_everything is
          # False.
          fuzz_target_to_unpack = self.fuzz_target
        else:
          fuzz_target_to_unpack = None

        # If the fuzz_target is None, this will return the full size.
        extracted_size = build.unpacked_size(fuzz_target=self.fuzz_target)

        if not _make_space(extracted_size, current_build_dir=base_build_dir):
          shell.clear_data_directories()
          logs.log_fatal_and_exit(
              'Failed to make space for build. '
              'Cleared all data directories to free up space, exiting.')

        # Unpack the local build archive.
        logs.info(f'Unpacking build archive {build_url} to {build_dir}.')
        trusted = not utils.is_oss_fuzz()

        build.unpack(
            build_dir=build_dir,
            fuzz_target=fuzz_target_to_unpack,
            trusted=trusted)

        _emit_job_build_retrieval_metric(unpack_start_time, 'unpack',
                                         self._build_type)

    except Exception as e:
      logs.error(f'Unable to unpack build archive {build_url}: {e}')
      return False

    if not self._unpack_everything:
      # If this is partial build due to selected build files, then mark it as
      # such so that it is not re-used.
      partial_build_file_path = os.path.join(build_dir, PARTIAL_BUILD_FILE)
      utils.write_data_to_file('', partial_build_file_path)

    _emit_job_build_retrieval_metric(start_time, 'total', self._build_type)
    elapsed_time = time.time() - start_time

    elapsed_mins = elapsed_time / 60.
    log_func = logs.warning if elapsed_time > UNPACK_TIME_LIMIT else logs.info
    log_func(f'Build took {elapsed_mins:0.02f} minutes to unpack.')

    return True

  def _get_fuzz_targets_from_dir(self, build_dir):
    """Get iterator of fuzz targets from build dir."""
    # Import here as this path is not available in App Engine context.
    from clusterfuzz._internal.bot.fuzzers import utils as fuzzer_utils

    for path in fuzzer_utils.get_fuzz_targets(build_dir):
      yield fuzzer_utils.normalize_target_name(path)

  def setup(self):
    """Set up the build on disk, and set all the necessary environment
    variables. Should return whether or not build setup succeeded."""
    raise NotImplementedError

  @property
  def build_dir(self):
    """The build directory. Usually a subdirectory of base_build_dir."""
    raise NotImplementedError

  @property
  def fuzz_targets(self):
    if not self._fuzz_targets and self._unpack_everything:
      # we can lazily compute that when unpacking the whole archive, since we
      # know all the fuzzers will be in the build directory.
      start_time = time.time()
      self._fuzz_targets = list(self._get_fuzz_targets_from_dir(self.build_dir))
      _emit_job_build_retrieval_metric(start_time, 'list_fuzz_targets',
                                       self._build_type)
    return self._fuzz_targets

  def exists(self):
    """Check if build already exists."""
    revision_file = os.path.join(self.build_dir, REVISION_FILE_NAME)
    if os.path.exists(revision_file):
      with open(revision_file) as file_handle:
        try:
          current_revision = int(file_handle.read())
        except ValueError:
          current_revision = -1

      # We have the revision required locally, no more work to do, other than
      # setting application path environment variables.
      if self.revision == current_revision:
        return True

    return False

  def delete(self):
    """Delete this build."""
    # This overrides BaseBuild.delete (which deletes the entire base build
    # directory) to delete this specific build.
    shell.remove_directory(self.build_dir)

  def _write_revision(self):
    revision_file = os.path.join(self.build_dir, REVISION_FILE_NAME)
    revisions.write_revision_to_revision_file(revision_file, self.revision)

  def _setup_application_path(self,
                              build_dir=None,
                              app_path='APP_PATH',
                              build_update=False):
    """Sets up APP_PATH environment variables for revision build."""
    logs.info('Setup application path.')

    if not build_dir:
      build_dir = self.build_dir

    # Make sure to initialize so that we don't carry stale values
    # in case of errors. app_path can be APP_PATH or APP_PATH_DEBUG.
    app_path = self.env_prefix + app_path
    environment.set_value(app_path, '')
    environment.set_value(self.env_prefix + 'APP_DIR', '')
    environment.set_value(self.env_prefix + 'BUILD_DIR', build_dir)
    environment.set_value(self.env_prefix + 'GN_ARGS_PATH', '')
    environment.set_value(self.env_prefix + 'LLVM_SYMBOLIZER_PATH',
                          environment.get_default_tool_path('llvm-symbolizer'))

    # Initialize variables.
    fuzzer_directory = environment.get_value('FUZZER_DIR')
    search_directories = [build_dir]
    if fuzzer_directory:
      search_directories.append(fuzzer_directory)

    set_environment_vars(
        search_directories, app_path=app_path, env_prefix=self.env_prefix)

    absolute_file_path = environment.get_value(app_path)
    app_directory = environment.get_value(self.env_prefix + 'APP_DIR')

    if not absolute_file_path:
      return

    # Set the symlink if needed.
    symbolic_link_target = environment.get_value(self.env_prefix +
                                                 'SYMBOLIC_LINK')
    if symbolic_link_target:
      os.system('mkdir --parents %s' % os.path.dirname(symbolic_link_target))
      os.system('rm %s' % symbolic_link_target)
      os.system('ln -s %s %s' % (app_directory, symbolic_link_target))

    if utils.is_chromium():
      # Use deterministic fonts when available. See crbug.com/822737.
      # For production builds (stable, beta), assume that they support it.
      if not isinstance(self.revision, int) or self.revision >= 635076:
        environment.set_value('FONTCONFIG_SYSROOT', app_directory)
      else:
        # Remove if set during previous iterations of regression testing.
        environment.remove_key('FONTCONFIG_SYSROOT')

    if not environment.is_android():
      return

    android.device.update_build(absolute_file_path, force_update=build_update)


class RegularBuild(Build):
  """Represents a regular build."""

  def __init__(self,
               base_build_dir,
               revision,
               build_url,
               build_prefix='',
               fuzz_target=None,
               http_build_url=None):
    """RegularBuild constructor. See Build constructor for other parameters.

    Args:
        http_build_url: the http build URL. E.g.
        http://storage.com/foo/bar.zip. Defaults to None.
        build_url: the GCS bucket URL where the build is stored. E.g.
        gs://foo/bar.zip.
    """
    super().__init__(
        base_build_dir, revision, build_prefix, fuzz_target=fuzz_target)
    self.build_url = build_url
    self.http_build_url = http_build_url

    if build_prefix:
      self.build_dir_name = build_prefix.lower()
    else:
      self.build_dir_name = 'revisions'

    self._build_dir = os.path.join(self.base_build_dir, self.build_dir_name)

  @property
  def build_dir(self):
    return self._build_dir

  def setup(self):
    """Sets up build with a particular revision."""
    self._pre_setup()
    environment.set_value(self.env_prefix + 'BUILD_URL', self.build_url)

    logs.info(f'Retrieving build r{self.revision} from {self.build_url}.')
    build_update = not self.exists()
    if build_update:
      if not self._unpack_build(self.base_build_dir, self.build_dir,
                                self.build_url, self.http_build_url):
        return False

      logs.info('Retrieved build r%d.' % self.revision)
    else:
      # We have the revision required locally, no more work to do, other than
      # setting application path environment variables.
      logs.info('Build already exists.')

      # This list will be incomplete because the directory on disk does not have
      # all fuzz targets. This is fine. The way fuzz_targets are added to db, it
      # does not clobber complete lists.
      assert self._fuzz_targets is None
      self._fuzz_targets = list(self._get_fuzz_targets_from_dir(self.build_dir))

    self._setup_application_path(build_update=build_update)
    self._post_setup_success(update_revision=build_update)
    return True


class SplitTargetBuild(RegularBuild):
  """Represents a split target build."""

  def setup(self, *args, **kwargs):
    result = super().setup(*args, **kwargs)
    self._fuzz_targets = list(_split_target_build_list_targets())
    return result


class FuchsiaBuild(RegularBuild):
  """Represents a Fuchsia build."""

  def _get_fuzz_targets_from_dir(self, build_dir):
    """A running instance is required to enumerate targets so this is a
    no-op."""
    return []

  def setup(self):
    """Fuchsia build setup."""
    # Prevent App Engine import issues.
    from clusterfuzz._internal.platforms import fuchsia

    environment.set_value('FUCHSIA_RESOURCES_DIR', self.build_dir)

    # Allow superclass's setup() to unpack the build
    assert environment.get_value('UNPACK_ALL_FUZZ_TARGETS_AND_FILES'), \
        'Fuchsia does not support partial unpacks'
    result = super().setup()
    if not result:
      return result

    # Verify that undercoat is available in this build
    fuchsia.undercoat.validate_api_version()

    # Kill any stale undercoat instances (currently, this is in fact the only
    # path through which instances are shut down)
    fuchsia.undercoat.stop_all()

    logs.info('Starting Fuchsia instance.')
    handle = fuchsia.undercoat.start_instance()
    environment.set_value('FUCHSIA_INSTANCE_HANDLE', handle)

    # Select a fuzzer, now that a list is available
    fuzz_targets = fuchsia.undercoat.list_fuzzers(handle)
    self._fuzz_targets = list(fuzz_targets)
    return True


class SymbolizedBuild(Build):
  """Symbolized build."""

  def __init__(self, base_build_dir, revision, release_build_url,
               debug_build_url):
    super().__init__(base_build_dir, revision)
    self._build_dir = os.path.join(self.base_build_dir, 'symbolized')
    self.release_build_dir = os.path.join(self.build_dir, 'release')
    self.debug_build_dir = os.path.join(self.build_dir, 'debug')

    self.release_build_url = release_build_url
    self.debug_build_url = debug_build_url

  @property
  def build_dir(self):
    return self._build_dir

  def _unpack_builds(self):
    """Download and unpack builds."""
    if not shell.remove_directory(self.build_dir, recreate=True):
      logs.error('Unable to clear symbolized build directory.')
      _handle_unrecoverable_error_on_windows()
      return False

    if not self.release_build_url and not self.debug_build_url:
      return False

    if self.release_build_url:
      # Expect self._build_type to be set in the constructor for Build
      assert self._build_type == 'release'
      if not self._unpack_build(self.base_build_dir, self.release_build_dir,
                                self.release_build_url):
        return False

    if self.debug_build_url:
      self._build_type = 'debug'
      if not self._unpack_build(self.base_build_dir, self.debug_build_dir,
                                self.debug_build_url):
        return False

    return True

  def setup(self):
    self._pre_setup()
    logs.info('Retrieving symbolized build r%d.' % self.revision)

    build_update = not self.exists()
    if build_update:
      if not self._unpack_builds():
        return False

      logs.info('Retrieved symbolized build r%d.' % self.revision)
    else:
      logs.info('Build already exists.')

    if self.release_build_url:
      self._setup_application_path(
          self.release_build_dir, build_update=build_update)
      environment.set_value('BUILD_URL', self.release_build_url)

    if self.debug_build_url:
      # Note: this will override LLVM_SYMBOLIZER_PATH, APP_DIR etc from the
      # previous release setup, which may not be desirable behaviour.
      self._setup_application_path(
          self.debug_build_dir, 'APP_PATH_DEBUG', build_update=build_update)

    self._post_setup_success(update_revision=build_update)
    return True


class CustomBuild(Build):
  """Custom binary."""

  def __init__(self, base_build_dir, custom_binary_key, custom_binary_filename,
               custom_binary_revision):
    super().__init__(base_build_dir, custom_binary_revision)
    self.custom_binary_key = custom_binary_key
    self.custom_binary_filename = custom_binary_filename
    self._build_dir = os.path.join(self.base_build_dir, 'custom')

  @property
  def build_dir(self):
    return self._build_dir

  def _unpack_custom_build(self):
    """Unpack the custom build."""
    if not shell.remove_directory(self.build_dir, recreate=True):
      logs.error('Unable to clear custom binary directory.')
      _handle_unrecoverable_error_on_windows()
      return False

    build_local_archive = os.path.join(self.build_dir,
                                       self.custom_binary_filename)
    custom_builds_bucket = local_config.ProjectConfig().get(
        'custom_builds.bucket')

    download_start_time = time.time()

    if custom_builds_bucket:
      directory = os.path.dirname(build_local_archive)
      if not os.path.exists(directory):
        os.makedirs(directory)
      gcs_path = f'/{custom_builds_bucket}/{self.custom_binary_key}'
      storage.copy_file_from(gcs_path, build_local_archive)
    elif not blobs.read_blob_to_disk(self.custom_binary_key,
                                     build_local_archive):
      return False

    _emit_job_build_retrieval_metric(download_start_time, 'download',
                                     self._build_type)
    # If custom binary is an archive, then unpack it.
    if archive.is_archive(self.custom_binary_filename):
      try:
        build = build_archive.open(build_local_archive)
      except:
        logs.error('Unable to open build archive %s.' % build_local_archive)
        return False
      if not _make_space(build.unpacked_size(), self.base_build_dir):
        # Remove downloaded archive to free up space and otherwise, it won't get
        # deleted until next job run.
        build.close()
        shell.remove_file(build_local_archive)

        logs.log_fatal_and_exit('Could not make space for build.')

      try:
        # Unpack belongs to the BuildArchive class
        unpack_start_time = time.time()
        build.unpack(self.build_dir, trusted=True)
        _emit_job_build_retrieval_metric(unpack_start_time, 'unpack',
                                         self._build_type)
      except:
        build.close()
        logs.error('Unable to unpack build archive %s.' % build_local_archive)
        return False

      build.close()
      # Remove the archive.
      shell.remove_file(build_local_archive)

    _emit_job_build_retrieval_metric(download_start_time, 'download',
                                     self._build_type)
    return True

  def setup(self):
    """Set up the custom binary for a particular job."""
    self._pre_setup()

    # Track the key for the custom binary so we can create a download link
    # later.
    environment.set_value('BUILD_KEY', self.custom_binary_key)

    logs.info('Retrieving custom binary build r%d.' % self.revision)

    revision_file = os.path.join(self.build_dir, REVISION_FILE_NAME)
    build_update = revisions.needs_update(revision_file, self.revision)

    if build_update:
      if not self._unpack_custom_build():
        return False

      logs.info('Retrieved custom binary build r%d.' % self.revision)
    else:
      logs.info('Build already exists.')

    self._fuzz_targets = list(self._get_fuzz_targets_from_dir(self.build_dir))
    self._setup_application_path(build_update=build_update)
    self._post_setup_success(update_revision=build_update)
    return True


def _sort_build_urls_by_revision(build_urls, bucket_path, reverse):
  """Return a sorted list of build url by revision."""
  base_url = os.path.dirname(bucket_path)
  file_pattern = os.path.basename(bucket_path)
  filename_by_revision_dict = {}

  _, base_path = storage.get_bucket_name_and_path(base_url)
  base_path_with_seperator = base_path + '/' if base_path else ''

  for build_url in build_urls:
    match_pattern = f'{base_path_with_seperator}({file_pattern})'
    match = re.match(match_pattern, build_url)
    if match:
      filename = match.group(1)
      revision = match.group(2)

      # Ensure that there are no duplicate revisions.
      if revision in filename_by_revision_dict:
        job_name = environment.get_value('JOB_NAME')
        raise errors.BadStateError(
            'Found duplicate revision %s when processing bucket. '
            'Bucket path is probably malformed for job %s.' % (revision,
                                                               job_name))

      filename_by_revision_dict[revision] = filename

  try:
    sorted_revisions = sorted(
        filename_by_revision_dict,
        reverse=reverse,
        key=lambda x: list(map(int, x.split('.'))))
  except:
    logs.warning(
        'Revision pattern is not an integer, falling back to string sort.')
    sorted_revisions = sorted(filename_by_revision_dict, reverse=reverse)

  sorted_build_urls = []
  for revision in sorted_revisions:
    filename = filename_by_revision_dict[revision]
    sorted_build_urls.append('%s/%s' % (base_url, filename))

  return sorted_build_urls


def get_build_urls_list(bucket_path, reverse=True):
  """Returns a sorted list of build urls from a bucket path."""
  if not bucket_path:
    return []

  base_url = os.path.dirname(bucket_path)
  if environment.is_running_on_app_engine():
    build_urls = list(storage.list_blobs(base_url))
  else:
    keys_directory = environment.get_value('BUILD_URLS_DIR')
    keys_filename = '%s.list' % utils.string_hash(bucket_path)
    keys_file_path = os.path.join(keys_directory, keys_filename)

    # For one task, keys file that is cached locally should be re-used.
    # Otherwise, we do waste lot of network bandwidth calling and getting the
    # same set of urls (esp for regression and progression testing).
    if not os.path.exists(keys_file_path):
      # Get url list by reading the GCS bucket.
      with open(keys_file_path, 'w') as f:
        for path in storage.list_blobs(base_url):
          f.write(path + '\n')
    data = utils.read_data_from_file(keys_file_path, eval_data=False)
    if not data:
      return []
    content = data.decode('utf-8')
    if not content:
      return []

    build_urls = content.splitlines()

  return _sort_build_urls_by_revision(build_urls, bucket_path, reverse)


def get_primary_bucket_path():
  """Get the main bucket path for the current job."""
  release_build_bucket_path = environment.get_value('RELEASE_BUILD_BUCKET_PATH')
  if release_build_bucket_path:
    return release_build_bucket_path

  fuzz_target_build_bucket_path = get_bucket_path(
      'FUZZ_TARGET_BUILD_BUCKET_PATH')

  if fuzz_target_build_bucket_path:
    fuzz_target = environment.get_value('FUZZ_TARGET')
    if not fuzz_target:
      raise BuildManagerError('FUZZ_TARGET is not defined.')

    return _full_fuzz_target_path(fuzz_target_build_bucket_path, fuzz_target)

  raise BuildManagerError(
      'RELEASE_BUILD_BUCKET_PATH or FUZZ_TARGET_BUILD_BUCKET_PATH '
      'needs to be defined.')


def get_revisions_list(bucket_path, bad_revisions, testcase=None):
  """Returns a sorted ascending list of revisions from a bucket path, excluding
  bad build revisions. Testcase crash revision is not excluded from the list
  even if it appears in the bad_revisions list."""
  revision_pattern = revisions.revision_pattern_from_build_bucket_path(
      bucket_path)

  revision_urls = get_build_urls_list(bucket_path, reverse=False)
  if not revision_urls:
    return None

  # Parse the revisions out of the build urls.
  revision_list = []
  for url in revision_urls:
    match = re.match(revision_pattern, url)
    if match:
      revision = revisions.convert_revision_to_integer(match.group(1))
      revision_list.append(revision)

  for bad_revision in bad_revisions:
    # Don't remove testcase revision even if it is in bad build list. This
    # usually happens when a bad bot sometimes marks a particular revision as
    # bad due to flakiness.
    if testcase and bad_revision == testcase.crash_revision:
      continue

    if bad_revision in revision_list:
      revision_list.remove(bad_revision)

  return revision_list


def get_job_bad_revisions():
  job_type = environment.get_value('JOB_NAME')

  bad_builds = list(
      ndb_utils.get_all_from_query(
          data_types.BuildMetadata.query(
              ndb_utils.is_true(data_types.BuildMetadata.bad_build),
              data_types.BuildMetadata.job_type == job_type)))
  return [build.revision for build in bad_builds]


def _base_fuzz_target_name(target_name):
  """Get the base fuzz target name "X" from "X@Y"."""
  return target_name.split('@')[0]


def _get_targets_list(bucket_path):
  """Get the target list for a given fuzz target bucket path. This is done by
  reading the targets.list file, which contains a list of the currently active
  fuzz targets."""
  bucket_dir_path = os.path.dirname(os.path.dirname(bucket_path))
  targets_list_path = os.path.join(bucket_dir_path, TARGETS_LIST_FILENAME)
  data = storage.read_data(targets_list_path)
  if not data:
    return None

  # Filter out targets which are not yet built.
  targets = data.decode('utf-8').splitlines()
  listed_targets = {
      os.path.basename(path.rstrip('/'))
      for path in storage.list_blobs(bucket_dir_path, recursive=False)
  }
  return [t for t in targets if _base_fuzz_target_name(t) in listed_targets]


def _full_fuzz_target_path(bucket_path, fuzz_target):
  """Get the full fuzz target bucket path."""
  return bucket_path.replace('%TARGET%', _base_fuzz_target_name(fuzz_target))


def _setup_split_targets_build(bucket_path, fuzz_target, revision=None):
  """Set up targets build."""
  bucket_path = environment.get_value('FUZZ_TARGET_BUILD_BUCKET_PATH')
  if not fuzz_target:
    raise BuildManagerError(
        f'Failed to choose a fuzz target (path={bucket_path}).')

  # Check this so that we handle deleted targets properly.
  targets_list = _get_targets_list(bucket_path)
  if fuzz_target not in targets_list:
    raise errors.BuildNotFoundError(revision, environment.get_value('JOB_NAME'))

  fuzz_target_bucket_path = _full_fuzz_target_path(bucket_path, fuzz_target)
  if not revision:
    revision = _get_latest_revision([fuzz_target_bucket_path])

  return setup_regular_build(
      revision, bucket_path=fuzz_target_bucket_path, fuzz_target=fuzz_target)


def _get_latest_revision(bucket_paths):
  """Get the latest revision."""
  build_urls = []
  for bucket_path in bucket_paths:
    urls_list = get_build_urls_list(bucket_path)
    if not urls_list:
      logs.error('Error getting list of build urls from %s.' % bucket_path)
      return None

    build_urls.append(BuildUrls(bucket_path=bucket_path, urls_list=urls_list))

  if len(build_urls) == 0:
    logs.error(
        'Attempted to get latest revision, but no build urls were found.')
    return None

  main_build_urls = build_urls[0]
  other_build_urls = build_urls[1:]

  revision_pattern = revisions.revision_pattern_from_build_bucket_path(
      main_build_urls.bucket_path)
  for build_url in main_build_urls.urls_list:
    match = re.match(revision_pattern, build_url)
    if not match:
      continue

    revision = revisions.convert_revision_to_integer(match.group(1))
    if (not other_build_urls or all(
        revisions.find_build_url(url.bucket_path, url.urls_list, revision)
        for url in other_build_urls)):
      return revision

  return None


def _emit_build_age_metric(gcs_path):
  """Emits a metric to track the age of a build."""
  try:
    last_update_time = storage.get(gcs_path).get('updated')
    # TODO(vitorguidi): standardize return type between fs and gcs.
    if isinstance(last_update_time, str):
      # storage.get returns two different types for the updated field:
      # the gcs api returns string, and the local filesystem implementation
      # returns a datetime.datetime object normalized for UTC.
      last_update_time = datetime.datetime.fromisoformat(last_update_time)
    now = datetime.datetime.now(datetime.timezone.utc)
    elapsed_time = now - last_update_time
    elapsed_time_in_hours = elapsed_time.total_seconds() / 3600
    # Fuzz targets do not apply for custom builds
    labels = {
        'job': os.getenv('JOB_NAME'),
        'platform': environment.platform(),
        'task': os.getenv('TASK_NAME'),
    }
    monitoring_metrics.JOB_BUILD_AGE.add(elapsed_time_in_hours, labels)
    # This field is expected as a datetime object
    # https://cloud.google.com/storage/docs/json_api/v1/objects#resource
  except Exception as e:
    logs.error(f'Failed to emit build age metric for {gcs_path}: {e}')


def _emit_build_revision_metric(revision):
  """Emits a gauge metric to track the build revision."""
  monitoring_metrics.JOB_BUILD_REVISION.set(
      revision,
      labels={
          'job': os.getenv('JOB_NAME'),
          'platform': environment.platform(),
          'task': os.getenv('TASK_NAME'),
      })


def _get_build_url(bucket_path: Optional[str], revision: int,
                   job_type: Optional[str]):
  """Returns the GCS url for a build, given a bucket path and revision"""
  build_urls = get_build_urls_list(bucket_path)
  if not build_urls:
    logs.error('Error getting build urls for job %s.' % job_type)
    return None
  build_url = revisions.find_build_url(bucket_path, build_urls, revision)
  if not build_url:
    logs.error(
        'Error getting build url for job %s (r%d).' % (job_type, revision))
    return None
  return build_url


def _get_build_bucket_paths():
  """Returns gcs bucket endpoints that contain the build of interest."""
  bucket_paths = []
  for env_var in DEFAULT_BUILD_BUCKET_PATH_ENV_VARS:
    bucket_path = get_bucket_path(env_var)
    if bucket_path:
      bucket_paths.append(bucket_path)
    else:
      logs.info('Bucket path not found for %s' % env_var)
  return bucket_paths


def setup_trunk_build(fuzz_target, build_prefix=None):
  """Sets up latest trunk build."""
  bucket_paths = _get_build_bucket_paths()
  if not bucket_paths:
    logs.error('Attempted a trunk build, but no bucket paths were found.')
    return None
  latest_revision = _get_latest_revision(bucket_paths)
  if latest_revision is None:
    logs.error('Unable to find a matching revision.')
    return None

  build = setup_regular_build(
      latest_revision,
      bucket_path=bucket_paths[0],
      build_prefix=build_prefix,
      fuzz_target=fuzz_target)
  if not build:
    logs.error('Failed to set up a build.')
    return None

  return build


def setup_regular_build(revision,
                        bucket_path=None,
                        build_prefix='',
                        fuzz_target=None) -> Optional[RegularBuild]:
  """Sets up build with a particular revision."""
  if not bucket_path:
    # Bucket path can be customized, otherwise get it from the default env var.
    bucket_path = get_bucket_path('RELEASE_BUILD_BUCKET_PATH')

  job_type = environment.get_value('JOB_NAME')
  build_url = _get_build_url(bucket_path, revision, job_type)

  if not build_url:
    return None

  all_bucket_paths = _get_build_bucket_paths()
  latest_revision = _get_latest_revision(all_bucket_paths)

  if revision == latest_revision:
    _emit_build_age_metric(build_url)
    _emit_build_revision_metric(revision)

  # build_url points to a GCP bucket, and we're only converting it to its HTTP
  # endpoint so that we can use remote unzipping.
  http_build_url = build_url.replace('gs://', 'https://storage.googleapis.com/')
  base_build_dir = _base_build_dir(bucket_path)

  build_class = RegularBuild
  if environment.is_trusted_host():
    from clusterfuzz._internal.bot.untrusted_runner import build_setup_host
    build_class = build_setup_host.RemoteRegularBuild
  elif environment.platform() == 'FUCHSIA':
    build_class = FuchsiaBuild
  elif get_bucket_path('FUZZ_TARGET_BUILD_BUCKET_PATH'):
    build_class = SplitTargetBuild

  result = None
  build = build_class(
      base_build_dir,
      revision,
      build_url,
      build_prefix=build_prefix,
      fuzz_target=fuzz_target,
      http_build_url=http_build_url)
  if build.setup():
    result = build
  else:
    return None

  # Additional binaries to pull (for fuzzing engines such as Centipede).
  extra_bucket_path = get_bucket_path('EXTRA_BUILD_BUCKET_PATH')
  if extra_bucket_path:
    # Import here as this path is not available in App Engine context.
    from clusterfuzz._internal.bot.fuzzers import utils as fuzzer_utils
    extra_build_urls = get_build_urls_list(extra_bucket_path)
    extra_build_url = revisions.find_build_url(extra_bucket_path,
                                               extra_build_urls, revision)
    if not extra_build_url:
      logs.error('Error getting extra build url for job %s (r%d).' % (job_type,
                                                                      revision))
      return None

    build = build_class(
        build.build_dir,  # Store inside the main build.
        revision,
        extra_build_url,
        build_prefix=fuzzer_utils.EXTRA_BUILD_DIR)
    if not build.setup():
      return None

  return result


def setup_symbolized_builds(revision):
  """Set up symbolized release and debug build."""
  sym_release_build_bucket_path = environment.get_value(
      'SYM_RELEASE_BUILD_BUCKET_PATH')
  sym_debug_build_bucket_path = environment.get_value(
      'SYM_DEBUG_BUILD_BUCKET_PATH')

  sym_release_build_urls = get_build_urls_list(sym_release_build_bucket_path)
  sym_debug_build_urls = get_build_urls_list(sym_debug_build_bucket_path)

  # We should at least have a symbolized debug or release build.
  if not sym_release_build_urls and not sym_debug_build_urls:
    logs.error('Error getting list of symbolized build urls from (%s, %s).' %
               (sym_release_build_bucket_path, sym_debug_build_bucket_path))
    return None

  sym_release_build_url = revisions.find_build_url(
      sym_release_build_bucket_path, sym_release_build_urls, revision)
  sym_debug_build_url = revisions.find_build_url(sym_debug_build_bucket_path,
                                                 sym_debug_build_urls, revision)

  base_build_dir = _base_build_dir(sym_release_build_bucket_path)

  build_class = SymbolizedBuild
  if environment.is_trusted_host():
    from clusterfuzz._internal.bot.untrusted_runner import build_setup_host
    build_class = build_setup_host.RemoteSymbolizedBuild  # pylint: disable=no-member

  build = build_class(base_build_dir, revision, sym_release_build_url,
                      sym_debug_build_url)
  if build.setup():
    return build

  return None


def setup_custom_binary():
  """Set up the custom binary for a particular job."""
  job_name = environment.get_value('JOB_NAME')
  # Verify that this is really a custom binary job.
  job = data_types.Job.query(data_types.Job.name == job_name).get()
  if not job or not job.custom_binary_key or not job.custom_binary_filename:
    logs.error(
        'Job does not have a custom binary, even though CUSTOM_BINARY is set.')
    return False

  base_build_dir = _base_build_dir('')
  build = CustomBuild(base_build_dir, job.custom_binary_key,
                      job.custom_binary_filename, job.custom_binary_revision)

  if build.setup():
    return build

  return None


def setup_build(revision=0, fuzz_target=None):
  """Set up a custom or regular build based on revision."""
  result = _setup_build(revision, fuzz_target)
  if fuzz_target:
    # TODO(metzman): Remove this unjustifiable use of a mutable global
    # variable.
    environment.set_value('FUZZ_TARGET', fuzz_target)
  return result


def _setup_build(revision, fuzz_target):
  """Helper for setup_build, so setup_build can be sure to set FUZZ_TARGET on
  successful execution of this function."""
  # For custom binaries we always use the latest version. Revision is ignored.
  custom_binary = environment.get_value('CUSTOM_BINARY')
  if custom_binary:
    return setup_custom_binary()

  fuzz_target_build_bucket_path = get_bucket_path(
      'FUZZ_TARGET_BUILD_BUCKET_PATH')

  if fuzz_target_build_bucket_path:
    # Split fuzz target build.
    return _setup_split_targets_build(
        fuzz_target_build_bucket_path, fuzz_target, revision=revision)

  if revision:
    # Setup regular build with revision.
    return setup_regular_build(revision, fuzz_target=fuzz_target)

  # If no revision is provided, we default to a trunk build.
  return setup_trunk_build(fuzz_target=fuzz_target)


def is_custom_binary():
  """Determine if this is a custom binary."""
  return bool(environment.get_value('CUSTOM_BINARY'))


def has_symbolized_builds():
  """Return a bool on if job type has either a release or debug build for stack
  symbolization."""
  return (environment.get_value('SYM_RELEASE_BUILD_BUCKET_PATH') or
          environment.get_value('SYM_DEBUG_BUILD_BUCKET_PATH'))


def _set_rpaths_chrpath(binary_path, rpaths):
  """Set rpaths using chrpath."""
  chrpath = environment.get_default_tool_path('chrpath')
  if not chrpath:
    raise BuildManagerError('Failed to find chrpath')

  subprocess.check_output(
      [chrpath, '-r', ':'.join(rpaths), binary_path], stderr=subprocess.PIPE)


def _set_rpaths_patchelf(binary_path, rpaths):
  """Set rpaths using patchelf."""
  patchelf = shutil.which('patchelf')
  if not patchelf:
    raise BuildManagerError('Failed to find patchelf')

  subprocess.check_output(
      [patchelf, '--force-rpath', '--set-rpath', ':'.join(rpaths), binary_path],
      stderr=subprocess.PIPE)


def set_rpaths(binary_path, rpaths):
  """Set rpath of a binary."""
  # Patchelf handles rpath patching much better, and allows e.g. extending the
  # length of the rpath. However, it loads the entire binary into memory so
  # does not work for large binaries, so use chrpath for larger binaries.
  binary_size = os.path.getsize(binary_path)
  if binary_size >= PATCHELF_SIZE_LIMIT:
    _set_rpaths_chrpath(binary_path, rpaths)
  else:
    _set_rpaths_patchelf(binary_path, rpaths)


def get_rpaths(binary_path):
  """Get rpath of a binary."""
  chrpath = environment.get_default_tool_path('chrpath')
  if not chrpath:
    raise BuildManagerError('Failed to find chrpath')

  try:
    rpaths = subprocess.check_output(
        [chrpath, '-l', binary_path],
        stderr=subprocess.PIPE).strip().decode('utf-8')
  except subprocess.CalledProcessError as e:
    if b'no rpath or runpath tag found' in e.output:
      return []

    raise

  if rpaths:
    search_marker = 'RPATH='
    start_index = rpaths.index(search_marker) + len(search_marker)
    return rpaths[start_index:].split(':')

  return []


def _pick_random_fuzz_target_for_standard_build(target_weights):
  return set_random_fuzz_target_for_fuzzing_if_needed(target_weights.keys(),
                                                      target_weights)


def _split_target_build_list_targets():
  bucket_path = environment.get_value('FUZZ_TARGET_BUILD_BUCKET_PATH')
  targets_list = _get_targets_list(bucket_path)
  if not targets_list:
    raise BuildManagerError(
        f'No targets found in targets.list (path={bucket_path}).')
  return targets_list


def _pick_random_fuzz_target_for_split_build(target_weights):
  targets_list = _split_target_build_list_targets()
  fuzz_target = set_random_fuzz_target_for_fuzzing_if_needed(
      targets_list, target_weights)
  if not fuzz_target:
    bucket_path = environment.get_value('FUZZ_TARGET_BUILD_BUCKET_PATH')
    raise BuildManagerError(
        f'Failed to choose a fuzz target (path={bucket_path}).')
  return fuzz_target


def pick_random_fuzz_target(target_weights):
  if environment.get_value('FUZZ_TARGET_BUILD_BUCKET_PATH'):
    return _pick_random_fuzz_target_for_split_build(target_weights)
  return _pick_random_fuzz_target_for_standard_build(target_weights)


def check_app_path(app_path='APP_PATH') -> bool:
  """Check if APP_PATH is properly set."""
  # If APP_NAME is not set (e.g. for grey box jobs), then we don't need
  # APP_PATH.
  if not environment.get_value('APP_NAME'):
    logs.info('APP_NAME is not set.')
    return True
  logs.info('APP_NAME is set.')

  app_path_value = environment.get_value(app_path)
  logs.info(f'app_path: {app_path} {app_path_value}')
  return bool(app_path_value)


def get_bucket_path(name):
  """Return build bucket path, applying any set overrides."""
  bucket_path = environment.get_value(name)
  bucket_path = overrides.check_and_apply_overrides(
      bucket_path, overrides.PLATFORM_ID_TO_BUILD_PATH_KEY)
  return bucket_path
