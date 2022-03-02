#!/usr/bin/env python3
#
# Copyright (c) 2012 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Compile Android resources into an intermediate APK.

This can also generate an R.txt, and an .srcjar file containing the proper
final R.java class for all resource packages the APK depends on.

This will crunch images with aapt2.
"""

import argparse
import collections
import contextlib
import filecmp
import hashlib
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import zipfile
from xml.etree import ElementTree

from util import build_utils
from util import diff_utils
from util import manifest_utils
from util import parallel
from util import protoresources
from util import resource_utils


# Pngs that we shouldn't convert to webp. Please add rationale when updating.
_PNG_WEBP_EXCLUSION_PATTERN = re.compile('|'.join([
    # Crashes on Galaxy S5 running L (https://crbug.com/807059).
    r'.*star_gray\.png',
    # Android requires pngs for 9-patch images.
    r'.*\.9\.png',
    # Daydream requires pngs for icon files.
    r'.*daydream_icon_.*\.png'
]))


def _ParseArgs(args):
  """Parses command line options.

  Returns:
    An options object as from argparse.ArgumentParser.parse_args()
  """
  parser, input_opts, output_opts = resource_utils.ResourceArgsParser()

  input_opts.add_argument(
      '--aapt2-path', required=True, help='Path to the Android aapt2 tool.')
  input_opts.add_argument(
      '--android-manifest', required=True, help='AndroidManifest.xml path.')
  input_opts.add_argument(
      '--r-java-root-package-name',
      default='base',
      help='Short package name for this target\'s root R java file (ex. '
      'input of "base" would become gen.base_module). Defaults to "base".')
  group = input_opts.add_mutually_exclusive_group()
  group.add_argument(
      '--shared-resources',
      action='store_true',
      help='Make all resources in R.java non-final and allow the resource IDs '
      'to be reset to a different package index when the apk is loaded by '
      'another application at runtime.')
  group.add_argument(
      '--app-as-shared-lib',
      action='store_true',
      help='Same as --shared-resources, but also ensures all resource IDs are '
      'directly usable from the APK loaded as an application.')

  input_opts.add_argument(
      '--package-id',
      type=int,
      help='Decimal integer representing custom package ID for resources '
      '(instead of 127==0x7f). Cannot be used with --shared-resources.')

  input_opts.add_argument(
      '--package-name',
      help='Package name that will be used to create R class.')

  input_opts.add_argument(
      '--rename-manifest-package', help='Package name to force AAPT to use.')

  input_opts.add_argument(
      '--arsc-package-name',
      help='Package name to set in manifest of resources.arsc file. This is '
      'only used for apks under test.')

  input_opts.add_argument(
      '--shared-resources-allowlist',
      help='An R.txt file acting as a allowlist for resources that should be '
      'non-final and have their package ID changed at runtime in R.java. '
      'Implies and overrides --shared-resources.')

  input_opts.add_argument(
      '--shared-resources-allowlist-locales',
      default='[]',
      help='Optional GN-list of locales. If provided, all strings corresponding'
      ' to this locale list will be kept in the final output for the '
      'resources identified through --shared-resources-allowlist, even '
      'if --locale-allowlist is being used.')

  input_opts.add_argument(
      '--use-resource-ids-path',
      help='Use resource IDs generated by aapt --emit-ids.')

  input_opts.add_argument(
      '--extra-main-r-text-files',
      help='Additional R.txt files that will be added to the root R.java file, '
      'but not packaged in the generated resources.arsc. If these resources '
      'entries contain duplicate resources with the generated R.txt file, they '
      'must be identical.')

  input_opts.add_argument(
      '--debuggable',
      action='store_true',
      help='Whether to add android:debuggable="true".')

  input_opts.add_argument('--version-code', help='Version code for apk.')
  input_opts.add_argument('--version-name', help='Version name for apk.')
  input_opts.add_argument(
      '--min-sdk-version', required=True, help='android:minSdkVersion for APK.')
  input_opts.add_argument(
      '--target-sdk-version',
      required=True,
      help="android:targetSdkVersion for APK.")
  input_opts.add_argument(
      '--max-sdk-version',
      help="android:maxSdkVersion expected in AndroidManifest.xml.")
  input_opts.add_argument(
      '--manifest-package', help='Package name of the AndroidManifest.xml.')

  input_opts.add_argument(
      '--locale-allowlist',
      default='[]',
      help='GN list of languages to include. All other language configs will '
      'be stripped out. List may include a combination of Android locales '
      'or Chrome locales.')
  input_opts.add_argument(
      '--resource-exclusion-regex',
      default='',
      help='File-based filter for resources (applied before compiling)')
  input_opts.add_argument(
      '--resource-exclusion-exceptions',
      default='[]',
      help='GN list of globs that say which files to include even '
      'when --resource-exclusion-regex is set.')

  input_opts.add_argument(
      '--dependencies-res-zip-overlays',
      help='GN list with subset of --dependencies-res-zips to use overlay '
      'semantics for.')

  input_opts.add_argument(
      '--values-filter-rules',
      help='GN list of source_glob:regex for filtering resources after they '
      'are compiled. Use this to filter out entries within values/ files.')

  input_opts.add_argument('--png-to-webp', action='store_true',
                          help='Convert png files to webp format.')

  input_opts.add_argument('--webp-binary', default='',
                          help='Path to the cwebp binary.')
  input_opts.add_argument(
      '--webp-cache-dir', help='The directory to store webp image cache.')

  input_opts.add_argument(
      '--no-xml-namespaces',
      action='store_true',
      help='Whether to strip xml namespaces from processed xml resources.')

  output_opts.add_argument('--arsc-path', help='Apk output for arsc format.')
  output_opts.add_argument('--proto-path', help='Apk output for proto format.')
  group = input_opts.add_mutually_exclusive_group()

  output_opts.add_argument(
      '--info-path', help='Path to output info file for the partial apk.')

  output_opts.add_argument(
      '--srcjar-out',
      required=True,
      help='Path to srcjar to contain generated R.java.')

  output_opts.add_argument('--r-text-out',
                           help='Path to store the generated R.txt file.')

  output_opts.add_argument(
      '--proguard-file', help='Path to proguard.txt generated file.')

  output_opts.add_argument(
      '--proguard-file-main-dex',
      help='Path to proguard.txt generated file for main dex.')

  output_opts.add_argument(
      '--emit-ids-out', help='Path to file produced by aapt2 --emit-ids.')

  input_opts.add_argument(
      '--is-bundle-module',
      action='store_true',
      help='Whether resources are being generated for a bundle module.')

  input_opts.add_argument(
      '--uses-split',
      help='Value to set uses-split to in the AndroidManifest.xml.')

  input_opts.add_argument(
      '--extra-verification-manifest',
      help='Path to AndroidManifest.xml which should be merged into base '
      'manifest when performing verification.')

  diff_utils.AddCommandLineFlags(parser)
  options = parser.parse_args(args)

  resource_utils.HandleCommonOptions(options)

  options.locale_allowlist = build_utils.ParseGnList(options.locale_allowlist)
  options.shared_resources_allowlist_locales = build_utils.ParseGnList(
      options.shared_resources_allowlist_locales)
  options.resource_exclusion_exceptions = build_utils.ParseGnList(
      options.resource_exclusion_exceptions)
  options.dependencies_res_zip_overlays = build_utils.ParseGnList(
      options.dependencies_res_zip_overlays)
  options.values_filter_rules = build_utils.ParseGnList(
      options.values_filter_rules)
  options.extra_main_r_text_files = build_utils.ParseGnList(
      options.extra_main_r_text_files)

  if not options.arsc_path and not options.proto_path:
    parser.error('One of --arsc-path or --proto-path is required.')

  if options.package_id and options.shared_resources:
    parser.error('--package-id and --shared-resources are mutually exclusive')

  return options


def _IterFiles(root_dir):
  for root, _, files in os.walk(root_dir):
    for f in files:
      yield os.path.join(root, f)


def _RenameLocaleResourceDirs(resource_dirs, path_info):
  """Rename locale resource directories into standard names when necessary.

  This is necessary to deal with the fact that older Android releases only
  support ISO 639-1 two-letter codes, and sometimes even obsolete versions
  of them.

  In practice it means:
    * 3-letter ISO 639-2 qualifiers are renamed under a corresponding
      2-letter one. E.g. for Filipino, strings under values-fil/ will be moved
      to a new corresponding values-tl/ sub-directory.

    * Modern ISO 639-1 codes will be renamed to their obsolete variant
      for Indonesian, Hebrew and Yiddish (e.g. 'values-in/ -> values-id/).

    * Norwegian macrolanguage strings will be renamed to Bokmal (main
      Norway language). See http://crbug.com/920960. In practice this
      means that 'values-no/ -> values-nb/' unless 'values-nb/' already
      exists.

    * BCP 47 langauge tags will be renamed to an equivalent ISO 639-1
      locale qualifier if possible (e.g. 'values-b+en+US/ -> values-en-rUS').

  Args:
    resource_dirs: list of top-level resource directories.
  """
  for resource_dir in resource_dirs:
    ignore_dirs = {}
    for path in _IterFiles(resource_dir):
      locale = resource_utils.FindLocaleInStringResourceFilePath(path)
      if not locale:
        continue
      cr_locale = resource_utils.ToChromiumLocaleName(locale)
      if not cr_locale:
        continue  # Unsupported Android locale qualifier!?
      locale2 = resource_utils.ToAndroidLocaleName(cr_locale)
      if locale != locale2:
        path2 = path.replace('/values-%s/' % locale, '/values-%s/' % locale2)
        if path == path2:
          raise Exception('Could not substitute locale %s for %s in %s' %
                          (locale, locale2, path))

        # Ignore rather than rename when the destination resources config
        # already exists.
        # e.g. some libraries provide both values-nb/ and values-no/.
        # e.g. material design provides:
        # * res/values-rUS/values-rUS.xml
        # * res/values-b+es+419/values-b+es+419.xml
        config_dir = os.path.dirname(path2)
        already_has_renamed_config = ignore_dirs.get(config_dir)
        if already_has_renamed_config is None:
          # Cache the result of the first time the directory is encountered
          # since subsequent encounters will find the directory already exists
          # (due to the rename).
          already_has_renamed_config = os.path.exists(config_dir)
          ignore_dirs[config_dir] = already_has_renamed_config
        if already_has_renamed_config:
          continue

        build_utils.MakeDirectory(os.path.dirname(path2))
        shutil.move(path, path2)
        path_info.RegisterRename(
            os.path.relpath(path, resource_dir),
            os.path.relpath(path2, resource_dir))


def _ToAndroidLocales(locale_allowlist):
  """Converts the list of Chrome locales to Android config locale qualifiers.

  Args:
    locale_allowlist: A list of Chromium locale names.
  Returns:
    A set of matching Android config locale qualifier names.
  """
  ret = set()
  for locale in locale_allowlist:
    locale = resource_utils.ToAndroidLocaleName(locale)
    if locale is None or ('-' in locale and '-r' not in locale):
      raise Exception('Unsupported Chromium locale name: %s' % locale)
    ret.add(locale)
    # Always keep non-regional fall-backs.
    language = locale.split('-')[0]
    ret.add(language)

  return ret


def _MoveImagesToNonMdpiFolders(res_root, path_info):
  """Move images from drawable-*-mdpi-* folders to drawable-* folders.

  Why? http://crbug.com/289843
  """
  for src_dir_name in os.listdir(res_root):
    src_components = src_dir_name.split('-')
    if src_components[0] != 'drawable' or 'mdpi' not in src_components:
      continue
    src_dir = os.path.join(res_root, src_dir_name)
    if not os.path.isdir(src_dir):
      continue
    dst_components = [c for c in src_components if c != 'mdpi']
    assert dst_components != src_components
    dst_dir_name = '-'.join(dst_components)
    dst_dir = os.path.join(res_root, dst_dir_name)
    build_utils.MakeDirectory(dst_dir)
    for src_file_name in os.listdir(src_dir):
      if not os.path.splitext(src_file_name)[1] in ('.png', '.webp', ''):
        continue
      src_file = os.path.join(src_dir, src_file_name)
      dst_file = os.path.join(dst_dir, src_file_name)
      assert not os.path.lexists(dst_file)
      shutil.move(src_file, dst_file)
      path_info.RegisterRename(
          os.path.relpath(src_file, res_root),
          os.path.relpath(dst_file, res_root))


def _FixManifest(options, temp_dir, extra_manifest=None):
  """Fix the APK's AndroidManifest.xml.

  This adds any missing namespaces for 'android' and 'tools', and
  sets certains elements like 'platformBuildVersionCode' or
  'android:debuggable' depending on the content of |options|.

  Args:
    options: The command-line arguments tuple.
    temp_dir: A temporary directory where the fixed manifest will be written to.
    extra_manifest: Path to an AndroidManifest.xml file which will get merged
        into the application node of the base manifest.
  Returns:
    Tuple of:
     * Manifest path within |temp_dir|.
     * Original package_name.
     * Manifest package name.
  """
  def maybe_extract_version(j):
    try:
      return resource_utils.ExtractBinaryManifestValues(options.aapt2_path, j)
    except build_utils.CalledProcessError:
      return None

  android_sdk_jars = [j for j in options.include_resources
                      if os.path.basename(j) in ('android.jar',
                                                 'android_system.jar')]
  extract_all = [maybe_extract_version(j) for j in android_sdk_jars]
  successful_extractions = [x for x in extract_all if x]
  if len(successful_extractions) == 0:
    raise Exception(
        'Unable to find android SDK jar among candidates: %s'
            % ', '.join(android_sdk_jars))
  if len(successful_extractions) > 1:
    raise Exception(
        'Found multiple android SDK jars among candidates: %s'
            % ', '.join(android_sdk_jars))
  version_code, version_name = successful_extractions.pop()[:2]

  debug_manifest_path = os.path.join(temp_dir, 'AndroidManifest.xml')
  doc, manifest_node, app_node = manifest_utils.ParseManifest(
      options.android_manifest)

  if extra_manifest:
    _, extra_manifest_node, extra_app_node = manifest_utils.ParseManifest(
        extra_manifest)
    for node in extra_app_node:
      app_node.append(node)
    for node in extra_manifest_node:
      # DFM manifests have a bunch of tags we don't care about inside
      # <manifest>, so only take <queries>.
      if node.tag == 'queries':
        manifest_node.append(node)

  manifest_utils.AssertUsesSdk(manifest_node, options.min_sdk_version,
                               options.target_sdk_version)
  # We explicitly check that maxSdkVersion is set in the manifest since we don't
  # add it later like minSdkVersion and targetSdkVersion.
  manifest_utils.AssertUsesSdk(
      manifest_node,
      max_sdk_version=options.max_sdk_version,
      fail_if_not_exist=True)
  manifest_utils.AssertPackage(manifest_node, options.manifest_package)

  manifest_node.set('platformBuildVersionCode', version_code)
  manifest_node.set('platformBuildVersionName', version_name)

  orig_package = manifest_node.get('package')
  fixed_package = orig_package
  if options.arsc_package_name:
    manifest_node.set('package', options.arsc_package_name)
    fixed_package = options.arsc_package_name

  if options.debuggable:
    app_node.set('{%s}%s' % (manifest_utils.ANDROID_NAMESPACE, 'debuggable'),
                 'true')

  if options.uses_split:
    uses_split = ElementTree.SubElement(manifest_node, 'uses-split')
    uses_split.set('{%s}name' % manifest_utils.ANDROID_NAMESPACE,
                   options.uses_split)

  # Make sure the min-sdk condition is not less than the min-sdk of the bundle.
  for min_sdk_node in manifest_node.iter('{%s}min-sdk' %
                                         manifest_utils.DIST_NAMESPACE):
    dist_value = '{%s}value' % manifest_utils.DIST_NAMESPACE
    if int(min_sdk_node.get(dist_value)) < int(options.min_sdk_version):
      min_sdk_node.set(dist_value, options.min_sdk_version)

  manifest_utils.SaveManifest(doc, debug_manifest_path)
  return debug_manifest_path, orig_package, fixed_package


def _CreateKeepPredicate(resource_exclusion_regex,
                         resource_exclusion_exceptions):
  """Return a predicate lambda to determine which resource files to keep.

  Args:
    resource_exclusion_regex: A regular expression describing all resources
      to exclude, except if they are mip-maps, or if they are listed
      in |resource_exclusion_exceptions|.
    resource_exclusion_exceptions: A list of glob patterns corresponding
      to exceptions to the |resource_exclusion_regex|.
  Returns:
    A lambda that takes a path, and returns true if the corresponding file
    must be kept.
  """
  predicate = lambda path: os.path.basename(path)[0] != '.'
  if resource_exclusion_regex == '':
    # Do not extract dotfiles (e.g. ".gitkeep"). aapt ignores them anyways.
    return predicate

  # A simple predicate that only removes (returns False for) paths covered by
  # the exclusion regex or listed as exceptions.
  return lambda path: (
      not re.search(resource_exclusion_regex, path) or
      build_utils.MatchesGlob(path, resource_exclusion_exceptions))


def _ComputeSha1(path):
  with open(path, 'rb') as f:
    data = f.read()
  return hashlib.sha1(data).hexdigest()


def _ConvertToWebPSingle(png_path, cwebp_binary, cwebp_version, webp_cache_dir):
  sha1_hash = _ComputeSha1(png_path)

  # The set of arguments that will appear in the cache key.
  quality_args = ['-m', '6', '-q', '100', '-lossless']

  webp_cache_path = os.path.join(
      webp_cache_dir, '{}-{}-{}'.format(sha1_hash, cwebp_version,
                                        ''.join(quality_args)))
  # No need to add .webp. Android can load images fine without them.
  webp_path = os.path.splitext(png_path)[0]

  cache_hit = os.path.exists(webp_cache_path)
  if cache_hit:
    os.link(webp_cache_path, webp_path)
  else:
    # We place the generated webp image to webp_path, instead of in the
    # webp_cache_dir to avoid concurrency issues.
    args = [cwebp_binary, png_path, '-o', webp_path, '-quiet'] + quality_args
    subprocess.check_call(args)

    try:
      os.link(webp_path, webp_cache_path)
    except OSError:
      # Because of concurrent run, a webp image may already exists in
      # webp_cache_path.
      pass

  os.remove(png_path)
  original_dir = os.path.dirname(os.path.dirname(png_path))
  rename_tuple = (os.path.relpath(png_path, original_dir),
                  os.path.relpath(webp_path, original_dir))
  return rename_tuple, cache_hit


def _ConvertToWebP(cwebp_binary, png_paths, path_info, webp_cache_dir):
  cwebp_version = subprocess.check_output([cwebp_binary, '-version']).rstrip()
  shard_args = [(f, ) for f in png_paths
                if not _PNG_WEBP_EXCLUSION_PATTERN.match(f)]

  build_utils.MakeDirectory(webp_cache_dir)
  results = parallel.BulkForkAndCall(_ConvertToWebPSingle,
                                     shard_args,
                                     cwebp_binary=cwebp_binary,
                                     cwebp_version=cwebp_version,
                                     webp_cache_dir=webp_cache_dir)
  total_cache_hits = 0
  for rename_tuple, cache_hit in results:
    path_info.RegisterRename(*rename_tuple)
    total_cache_hits += int(cache_hit)

  logging.debug('png->webp cache: %d/%d', total_cache_hits, len(shard_args))


def _RemoveImageExtensions(directory, path_info):
  """Remove extensions from image files in the passed directory.

  This reduces binary size but does not affect android's ability to load the
  images.
  """
  for f in _IterFiles(directory):
    if (f.endswith('.png') or f.endswith('.webp')) and not f.endswith('.9.png'):
      path_with_extension = f
      path_no_extension = os.path.splitext(path_with_extension)[0]
      if path_no_extension != path_with_extension:
        shutil.move(path_with_extension, path_no_extension)
        path_info.RegisterRename(
            os.path.relpath(path_with_extension, directory),
            os.path.relpath(path_no_extension, directory))


def _CompileSingleDep(index, dep_subdir, keep_predicate, aapt2_path,
                      partials_dir):
  unique_name = '{}_{}'.format(index, os.path.basename(dep_subdir))
  partial_path = os.path.join(partials_dir, '{}.zip'.format(unique_name))

  compile_command = [
      aapt2_path,
      'compile',
      # TODO(wnwen): Turn this on once aapt2 forces 9-patch to be crunched.
      # '--no-crunch',
      '--dir',
      dep_subdir,
      '-o',
      partial_path
  ]

  # There are resources targeting API-versions lower than our minapi. For
  # various reasons it's easier to let aapt2 ignore these than for us to
  # remove them from our build (e.g. it's from a 3rd party library).
  build_utils.CheckOutput(
      compile_command,
      stderr_filter=lambda output: build_utils.FilterLines(
          output, r'ignoring configuration .* for (styleable|attribute)'))

  # Filtering these files is expensive, so only apply filters to the partials
  # that have been explicitly targeted.
  if keep_predicate:
    logging.debug('Applying .arsc filtering to %s', dep_subdir)
    protoresources.StripUnwantedResources(partial_path, keep_predicate)
  return partial_path


def _CreateValuesKeepPredicate(exclusion_rules, dep_subdir):
  patterns = [
      x[1] for x in exclusion_rules
      if build_utils.MatchesGlob(dep_subdir, [x[0]])
  ]
  if not patterns:
    return None

  regexes = [re.compile(p) for p in patterns]
  return lambda x: not any(r.search(x) for r in regexes)


def _CompileDeps(aapt2_path, dep_subdirs, dep_subdir_overlay_set, temp_dir,
                 exclusion_rules):
  partials_dir = os.path.join(temp_dir, 'partials')
  build_utils.MakeDirectory(partials_dir)

  job_params = [(i, dep_subdir,
                 _CreateValuesKeepPredicate(exclusion_rules, dep_subdir))
                for i, dep_subdir in enumerate(dep_subdirs)]

  # Filtering is slow, so ensure jobs with keep_predicate are started first.
  job_params.sort(key=lambda x: not x[2])
  partials = list(
      parallel.BulkForkAndCall(_CompileSingleDep,
                               job_params,
                               aapt2_path=aapt2_path,
                               partials_dir=partials_dir))

  partials_cmd = list()
  for i, partial in enumerate(partials):
    dep_subdir = job_params[i][1]
    if dep_subdir in dep_subdir_overlay_set:
      partials_cmd += ['-R']
    partials_cmd += [partial]
  return partials_cmd


def _CreateResourceInfoFile(path_info, info_path, dependencies_res_zips):
  for zip_file in dependencies_res_zips:
    zip_info_file_path = zip_file + '.info'
    if os.path.exists(zip_info_file_path):
      path_info.MergeInfoFile(zip_info_file_path)
  path_info.Write(info_path)


def _RemoveUnwantedLocalizedStrings(dep_subdirs, options):
  """Remove localized strings that should not go into the final output.

  Args:
    dep_subdirs: List of resource dependency directories.
    options: Command-line options namespace.
  """
  # Collect locale and file paths from the existing subdirs.
  # The following variable maps Android locale names to
  # sets of corresponding xml file paths.
  locale_to_files_map = collections.defaultdict(set)
  for directory in dep_subdirs:
    for f in _IterFiles(directory):
      locale = resource_utils.FindLocaleInStringResourceFilePath(f)
      if locale:
        locale_to_files_map[locale].add(f)

  all_locales = set(locale_to_files_map)

  # Set A: wanted locales, either all of them or the
  # list provided by --locale-allowlist.
  wanted_locales = all_locales
  if options.locale_allowlist:
    wanted_locales = _ToAndroidLocales(options.locale_allowlist)

  # Set B: shared resources locales, which is either set A
  # or the list provided by --shared-resources-allowlist-locales
  shared_resources_locales = wanted_locales
  shared_names_allowlist = set()
  if options.shared_resources_allowlist_locales:
    shared_names_allowlist = set(
        resource_utils.GetRTxtStringResourceNames(
            options.shared_resources_allowlist))

    shared_resources_locales = _ToAndroidLocales(
        options.shared_resources_allowlist_locales)

  # Remove any file that belongs to a locale not covered by
  # either A or B.
  removable_locales = (all_locales - wanted_locales - shared_resources_locales)
  for locale in removable_locales:
    for path in locale_to_files_map[locale]:
      os.remove(path)

  # For any locale in B but not in A, only keep the shared
  # resource strings in each file.
  for locale in shared_resources_locales - wanted_locales:
    for path in locale_to_files_map[locale]:
      resource_utils.FilterAndroidResourceStringsXml(
          path, lambda x: x in shared_names_allowlist)

  # For any locale in A but not in B, only keep the strings
  # that are _not_ from shared resources in the file.
  for locale in wanted_locales - shared_resources_locales:
    for path in locale_to_files_map[locale]:
      resource_utils.FilterAndroidResourceStringsXml(
          path, lambda x: x not in shared_names_allowlist)


def _FilterResourceFiles(dep_subdirs, keep_predicate):
  # Create a function that selects which resource files should be packaged
  # into the final output. Any file that does not pass the predicate will
  # be removed below.
  png_paths = []
  for directory in dep_subdirs:
    for f in _IterFiles(directory):
      if not keep_predicate(f):
        os.remove(f)
      elif f.endswith('.png'):
        png_paths.append(f)

  return png_paths


def _PackageApk(options, build):
  """Compile and link resources with aapt2.

  Args:
    options: The command-line options.
    build: BuildContext object.
  Returns:
    The manifest package name for the APK.
  """
  logging.debug('Extracting resource .zips')
  dep_subdirs = []
  dep_subdir_overlay_set = set()
  for dependency_res_zip in options.dependencies_res_zips:
    extracted_dep_subdirs = resource_utils.ExtractDeps([dependency_res_zip],
                                                       build.deps_dir)
    dep_subdirs += extracted_dep_subdirs
    if dependency_res_zip in options.dependencies_res_zip_overlays:
      dep_subdir_overlay_set.update(extracted_dep_subdirs)

  logging.debug('Applying locale transformations')
  path_info = resource_utils.ResourceInfoFile()
  _RenameLocaleResourceDirs(dep_subdirs, path_info)

  logging.debug('Applying file-based exclusions')
  keep_predicate = _CreateKeepPredicate(options.resource_exclusion_regex,
                                        options.resource_exclusion_exceptions)
  png_paths = _FilterResourceFiles(dep_subdirs, keep_predicate)

  if options.locale_allowlist or options.shared_resources_allowlist_locales:
    logging.debug('Applying locale-based string exclusions')
    _RemoveUnwantedLocalizedStrings(dep_subdirs, options)

  if png_paths and options.png_to_webp:
    logging.debug('Converting png->webp')
    _ConvertToWebP(options.webp_binary, png_paths, path_info,
                   options.webp_cache_dir)
  logging.debug('Applying drawable transformations')
  for directory in dep_subdirs:
    _MoveImagesToNonMdpiFolders(directory, path_info)
    _RemoveImageExtensions(directory, path_info)

  logging.debug('Running aapt2 compile')
  exclusion_rules = [x.split(':', 1) for x in options.values_filter_rules]
  partials = _CompileDeps(options.aapt2_path, dep_subdirs,
                          dep_subdir_overlay_set, build.temp_dir,
                          exclusion_rules)

  link_command = [
      options.aapt2_path,
      'link',
      '--auto-add-overlay',
      '--no-version-vectors',
      # Set SDK versions in case they are not set in the Android manifest.
      '--min-sdk-version',
      options.min_sdk_version,
      '--target-sdk-version',
      options.target_sdk_version,
      '--output-text-symbols',
      build.r_txt_path,
  ]

  for j in options.include_resources:
    link_command += ['-I', j]
  if options.version_code:
    link_command += ['--version-code', options.version_code]
  if options.version_name:
    link_command += ['--version-name', options.version_name]
  if options.proguard_file:
    link_command += ['--proguard', build.proguard_path]
    link_command += ['--proguard-minimal-keep-rules']
  if options.proguard_file_main_dex:
    link_command += ['--proguard-main-dex', build.proguard_main_dex_path]
  if options.emit_ids_out:
    link_command += ['--emit-ids', build.emit_ids_path]

  # Note: only one of --proto-format, --shared-lib or --app-as-shared-lib
  #       can be used with recent versions of aapt2.
  if options.shared_resources:
    link_command.append('--shared-lib')

  if options.no_xml_namespaces:
    link_command.append('--no-xml-namespaces')

  if options.package_id:
    link_command += [
        '--package-id',
        hex(options.package_id),
        '--allow-reserved-package-id',
    ]

  fixed_manifest, desired_manifest_package_name, fixed_manifest_package = (
      _FixManifest(options, build.temp_dir))
  if options.rename_manifest_package:
    desired_manifest_package_name = options.rename_manifest_package

  link_command += [
      '--manifest', fixed_manifest, '--rename-manifest-package',
      desired_manifest_package_name
  ]

  # Creates a .zip with AndroidManifest.xml, resources.arsc, res/*
  # Also creates R.txt
  if options.use_resource_ids_path:
    _CreateStableIdsFile(options.use_resource_ids_path, build.stable_ids_path,
                         fixed_manifest_package)
    link_command += ['--stable-ids', build.stable_ids_path]

  link_command += partials

  # We always create a binary arsc file first, then convert to proto, so flags
  # such as --shared-lib can be supported.
  link_command += ['-o', build.arsc_path]

  logging.debug('Starting: aapt2 link')
  link_proc = subprocess.Popen(link_command)

  # Create .res.info file in parallel.
  _CreateResourceInfoFile(path_info, build.info_path,
                          options.dependencies_res_zips)
  logging.debug('Created .res.info file')

  exit_code = link_proc.wait()
  logging.debug('Finished: aapt2 link')

  if options.shared_resources:
    logging.debug('Resolving styleables in R.txt')
    # Need to resolve references because unused resource removal tool does not
    # support references in R.txt files.
    resource_utils.ResolveStyleableReferences(build.r_txt_path)

  if exit_code:
    raise subprocess.CalledProcessError(exit_code, link_command)

  if options.proguard_file and (options.shared_resources
                                or options.app_as_shared_lib):
    # Make sure the R class associated with the manifest package does not have
    # its onResourcesLoaded method obfuscated or removed, so that the framework
    # can call it in the case where the APK is being loaded as a library.
    with open(build.proguard_path, 'a') as proguard_file:
      keep_rule = '''
                  -keep,allowoptimization class {package}.R {{
                    public static void onResourcesLoaded(int);
                  }}
                  '''.format(package=desired_manifest_package_name)
      proguard_file.write(textwrap.dedent(keep_rule))

  logging.debug('Running aapt2 convert')
  build_utils.CheckOutput([
      options.aapt2_path, 'convert', '--output-format', 'proto', '-o',
      build.proto_path, build.arsc_path
  ])

  # Workaround for b/147674078. This is only needed for WebLayer and does not
  # affect WebView usage, since WebView does not used dynamic attributes.
  if options.shared_resources:
    logging.debug('Hardcoding dynamic attributes')
    protoresources.HardcodeSharedLibraryDynamicAttributes(
        build.proto_path, options.is_bundle_module,
        options.shared_resources_allowlist)

    build_utils.CheckOutput([
        options.aapt2_path, 'convert', '--output-format', 'binary', '-o',
        build.arsc_path, build.proto_path
    ])

  return desired_manifest_package_name


@contextlib.contextmanager
def _CreateStableIdsFile(in_path, out_path, package_name):
  """Transforms a file generated by --emit-ids from another package.

  --stable-ids is generally meant to be used by different versions of the same
  package. To make it work for other packages, we need to transform the package
  name references to match the package that resources are being generated for.

  Note: This will fail if the package ID of the resources in
  |options.use_resource_ids_path| does not match the package ID of the
  resources being linked.
  """
  with open(in_path) as stable_ids_file:
    with open(out_path, 'w') as output_ids_file:
      output_stable_ids = re.sub(
          r'^.*?:',
          package_name + ':',
          stable_ids_file.read(),
          flags=re.MULTILINE)
      output_ids_file.write(output_stable_ids)


def _WriteOutputs(options, build):
  possible_outputs = [
      (options.srcjar_out, build.srcjar_path),
      (options.r_text_out, build.r_txt_path),
      (options.arsc_path, build.arsc_path),
      (options.proto_path, build.proto_path),
      (options.proguard_file, build.proguard_path),
      (options.proguard_file_main_dex, build.proguard_main_dex_path),
      (options.emit_ids_out, build.emit_ids_path),
      (options.info_path, build.info_path),
  ]

  for final, temp in possible_outputs:
    # Write file only if it's changed.
    if final and not (os.path.exists(final) and filecmp.cmp(final, temp)):
      shutil.move(temp, final)


def _CreateNormalizedManifestForVerification(options):
  with build_utils.TempDir() as tempdir:
    fixed_manifest, _, _ = _FixManifest(
        options, tempdir, extra_manifest=options.extra_verification_manifest)
    with open(fixed_manifest) as f:
      return manifest_utils.NormalizeManifest(f.read())


def main(args):
  build_utils.InitLogging('RESOURCE_DEBUG')
  args = build_utils.ExpandFileArgs(args)
  options = _ParseArgs(args)

  if options.expected_file:
    actual_data = _CreateNormalizedManifestForVerification(options)
    diff_utils.CheckExpectations(actual_data, options)
    if options.only_verify_expectations:
      return

  path = options.arsc_path or options.proto_path
  debug_temp_resources_dir = os.environ.get('TEMP_RESOURCES_DIR')
  if debug_temp_resources_dir:
    path = os.path.join(debug_temp_resources_dir, os.path.basename(path))
  else:
    # Use a deterministic temp directory since .pb files embed the absolute
    # path of resources: crbug.com/939984
    path = path + '.tmpdir'
  build_utils.DeleteDirectory(path)

  with resource_utils.BuildContext(
      temp_dir=path, keep_files=bool(debug_temp_resources_dir)) as build:

    manifest_package_name = _PackageApk(options, build)

    # If --shared-resources-allowlist is used, all the resources listed in the
    # corresponding R.txt file will be non-final, and an onResourcesLoaded()
    # will be generated to adjust them at runtime.
    #
    # Otherwise, if --shared-resources is used, the all resources will be
    # non-final, and an onResourcesLoaded() method will be generated too.
    #
    # Otherwise, all resources will be final, and no method will be generated.
    #
    rjava_build_options = resource_utils.RJavaBuildOptions()
    if options.shared_resources_allowlist:
      rjava_build_options.ExportSomeResources(
          options.shared_resources_allowlist)
      rjava_build_options.GenerateOnResourcesLoaded()
      if options.shared_resources:
        # The final resources will only be used in WebLayer, so hardcode the
        # package ID to be what WebLayer expects.
        rjava_build_options.SetFinalPackageId(
            protoresources.SHARED_LIBRARY_HARDCODED_ID)
    elif options.shared_resources or options.app_as_shared_lib:
      rjava_build_options.ExportAllResources()
      rjava_build_options.GenerateOnResourcesLoaded()

    custom_root_package_name = options.r_java_root_package_name
    grandparent_custom_package_name = None

    # Always generate an R.java file for the package listed in
    # AndroidManifest.xml because this is where Android framework looks to find
    # onResourcesLoaded() for shared library apks. While not actually necessary
    # for application apks, it also doesn't hurt.
    apk_package_name = manifest_package_name

    if options.package_name and not options.arsc_package_name:
      # Feature modules have their own custom root package name and should
      # inherit from the appropriate base module package. This behaviour should
      # not be present for test apks with an apk under test. Thus,
      # arsc_package_name is used as it is only defined for test apks with an
      # apk under test.
      custom_root_package_name = options.package_name
      grandparent_custom_package_name = options.r_java_root_package_name
      # Feature modules have the same manifest package as the base module but
      # they should not create an R.java for said manifest package because it
      # will be created in the base module.
      apk_package_name = None

    logging.debug('Creating R.srcjar')
    resource_utils.CreateRJavaFiles(
        build.srcjar_dir, apk_package_name, build.r_txt_path,
        options.extra_res_packages, rjava_build_options, options.srcjar_out,
        custom_root_package_name, grandparent_custom_package_name,
        options.extra_main_r_text_files)
    build_utils.ZipDir(build.srcjar_path, build.srcjar_dir)

    # Sanity check that the created resources have the expected package ID.
    logging.debug('Performing sanity check')
    if options.package_id:
      expected_id = options.package_id
    elif options.shared_resources:
      expected_id = 0
    else:
      expected_id = 127  # == '0x7f'.
    _, package_id = resource_utils.ExtractArscPackage(
        options.aapt2_path,
        build.arsc_path if options.arsc_path else build.proto_path)
    # When there are no resources, ExtractArscPackage returns (None, None), in
    # this case there is no need to check for matching package ID.
    if package_id is not None and package_id != expected_id:
      raise Exception(
          'Invalid package ID 0x%x (expected 0x%x)' % (package_id, expected_id))

    logging.debug('Copying outputs')
    _WriteOutputs(options, build)

  if options.depfile:
    depfile_deps = (options.dependencies_res_zips +
                    options.dependencies_res_zip_overlays +
                    options.extra_main_r_text_files + options.include_resources)
    build_utils.WriteDepfile(options.depfile, options.srcjar_out, depfile_deps)


if __name__ == '__main__':
  main(sys.argv[1:])
