# Copyright (c) 2014-present PlatformIO <contact@platformio.org>
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import absolute_import

import os
import shutil
import time
from os.path import (basename, expanduser, getmtime, isdir, isfile, join,
                     realpath, sep)

import jsonrpc  # pylint: disable=import-error

from platformio import exception, util
from platformio.commands.home.rpc.handlers.app import AppRPC
from platformio.commands.home.rpc.handlers.piocore import PIOCoreRPC
from platformio.compat import PY2, get_filesystem_encoding
from platformio.ide.projectgenerator import ProjectGenerator
from platformio.managers.platform import PlatformManager
from platformio.project.config import ProjectConfig
from platformio.project.helpers import (get_project_libdeps_dir,
                                        get_project_src_dir,
                                        is_platformio_project)


class ProjectRPC(object):

    @staticmethod
    def _get_projects(project_dirs=None):

        def _get_project_data(project_dir):
            data = {"boards": [], "envLibdepsDirs": [], "libExtraDirs": []}
            config = ProjectConfig(join(project_dir, "platformio.ini"))
            libdeps_dir = get_project_libdeps_dir()

            data['libExtraDirs'].extend(
                config.get("platformio", "lib_extra_dirs", []))

            for section in config.sections():
                if not section.startswith("env:"):
                    continue
                data['envLibdepsDirs'].append(join(libdeps_dir, section[4:]))
                if config.has_option(section, "board"):
                    data['boards'].append(config.get(section, "board"))
                data['libExtraDirs'].extend(
                    config.get(section, "lib_extra_dirs", []))

            # skip non existing folders and resolve full path
            for key in ("envLibdepsDirs", "libExtraDirs"):
                data[key] = [
                    expanduser(d) if d.startswith("~") else realpath(d)
                    for d in data[key] if isdir(d)
                ]

            return data

        def _path_to_name(path):
            return (sep).join(path.split(sep)[-2:])

        if not project_dirs:
            project_dirs = AppRPC.load_state()['storage']['recentProjects']

        result = []
        pm = PlatformManager()
        for project_dir in project_dirs:
            data = {}
            boards = []
            try:
                with util.cd(project_dir):
                    data = _get_project_data(project_dir)
            except exception.PlatformIOProjectException:
                continue

            for board_id in data.get("boards", []):
                name = board_id
                try:
                    name = pm.board_config(board_id)['name']
                except (exception.UnknownBoard, exception.UnknownPlatform):
                    pass
                boards.append({"id": board_id, "name": name})

            result.append({
                "path":
                project_dir,
                "name":
                _path_to_name(project_dir),
                "modified":
                int(getmtime(project_dir)),
                "boards":
                boards,
                "envLibStorages": [{
                    "name": basename(d),
                    "path": d
                } for d in data.get("envLibdepsDirs", [])],
                "extraLibStorages": [{
                    "name": _path_to_name(d),
                    "path": d
                } for d in data.get("libExtraDirs", [])]
            })
        return result

    def get_projects(self, project_dirs=None):
        return self._get_projects(project_dirs)

    def init(self, board, framework, project_dir):
        assert project_dir
        state = AppRPC.load_state()
        if not isdir(project_dir):
            os.makedirs(project_dir)
        args = ["init", "--project-dir", project_dir, "--board", board]
        if framework:
            args.extend(["--project-option", "framework = %s" % framework])
        if (state['storage']['coreCaller'] and state['storage']['coreCaller']
                in ProjectGenerator.get_supported_ides()):
            args.extend(["--ide", state['storage']['coreCaller']])
        d = PIOCoreRPC.call(args)
        d.addCallback(self._generate_project_main, project_dir, framework)
        return d

    @staticmethod
    def _generate_project_main(_, project_dir, framework):
        main_content = None
        if framework == "arduino":
            main_content = "\n".join([
                "#include <Arduino.h>",
                "",
                "void setup() {",
                "  // put your setup code here, to run once:",
                "}",
                "",
                "void loop() {",
                "  // put your main code here, to run repeatedly:",
                "}"
                ""
            ])   # yapf: disable
        elif framework == "mbed":
            main_content = "\n".join([
                "#include <mbed.h>",
                "",
                "int main() {",
                "",
                "  // put your setup code here, to run once:",
                "",
                "  while(1) {",
                "    // put your main code here, to run repeatedly:",
                "  }",
                "}",
                ""
            ])   # yapf: disable
        if not main_content:
            return project_dir
        with util.cd(project_dir):
            src_dir = get_project_src_dir()
            main_path = join(src_dir, "main.cpp")
            if isfile(main_path):
                return project_dir
            if not isdir(src_dir):
                os.makedirs(src_dir)
            with open(main_path, "w") as f:
                f.write(main_content.strip())
        return project_dir

    def import_arduino(self, board, use_arduino_libs, arduino_project_dir):
        board = str(board)
        if arduino_project_dir and PY2:
            arduino_project_dir = arduino_project_dir.encode(
                get_filesystem_encoding())
        # don't import PIO Project
        if is_platformio_project(arduino_project_dir):
            return arduino_project_dir

        is_arduino_project = any([
            isfile(
                join(arduino_project_dir,
                     "%s.%s" % (basename(arduino_project_dir), ext)))
            for ext in ("ino", "pde")
        ])
        if not is_arduino_project:
            raise jsonrpc.exceptions.JSONRPCDispatchException(
                code=4000,
                message="Not an Arduino project: %s" % arduino_project_dir)

        state = AppRPC.load_state()
        project_dir = join(state['storage']['projectsDir'],
                           time.strftime("%y%m%d-%H%M%S-") + board)
        if not isdir(project_dir):
            os.makedirs(project_dir)
        args = ["init", "--project-dir", project_dir, "--board", board]
        args.extend(["--project-option", "framework = arduino"])
        if use_arduino_libs:
            args.extend([
                "--project-option",
                "lib_extra_dirs = ~/Documents/Arduino/libraries"
            ])
        if (state['storage']['coreCaller'] and state['storage']['coreCaller']
                in ProjectGenerator.get_supported_ides()):
            args.extend(["--ide", state['storage']['coreCaller']])
        d = PIOCoreRPC.call(args)
        d.addCallback(self._finalize_arduino_import, project_dir,
                      arduino_project_dir)
        return d

    @staticmethod
    def _finalize_arduino_import(_, project_dir, arduino_project_dir):
        with util.cd(project_dir):
            src_dir = get_project_src_dir()
            if isdir(src_dir):
                util.rmtree_(src_dir)
            shutil.copytree(arduino_project_dir, src_dir)
        return project_dir

    @staticmethod
    def get_project_examples():
        result = []
        for manifest in PlatformManager().get_installed():
            examples_dir = join(manifest['__pkg_dir'], "examples")
            if not isdir(examples_dir):
                continue
            items = []
            for project_dir, _, __ in os.walk(examples_dir):
                project_description = None
                try:
                    config = ProjectConfig(join(project_dir, "platformio.ini"))
                    config.validate(silent=True)
                    project_description = config.get("platformio",
                                                     "description")
                except exception.PlatformIOProjectException:
                    continue

                path_tokens = project_dir.split(sep)
                items.append({
                    "name":
                    "/".join(path_tokens[path_tokens.index("examples") + 1:]),
                    "path":
                    project_dir,
                    "description":
                    project_description
                })
            result.append({
                "platform": {
                    "title": manifest['title'],
                    "version": manifest['version']
                },
                "items": sorted(items, key=lambda item: item['name'])
            })
        return sorted(result, key=lambda data: data['platform']['title'])

    @staticmethod
    def import_pio(project_dir):
        if project_dir and PY2:
            project_dir = project_dir.encode(get_filesystem_encoding())
        if not project_dir or not is_platformio_project(project_dir):
            raise jsonrpc.exceptions.JSONRPCDispatchException(
                code=4001,
                message="Not an PlatformIO project: %s" % project_dir)
        new_project_dir = join(
            AppRPC.load_state()['storage']['projectsDir'],
            time.strftime("%y%m%d-%H%M%S-") + basename(project_dir))
        shutil.copytree(project_dir, new_project_dir)

        state = AppRPC.load_state()
        args = ["init", "--project-dir", new_project_dir]
        if (state['storage']['coreCaller'] and state['storage']['coreCaller']
                in ProjectGenerator.get_supported_ides()):
            args.extend(["--ide", state['storage']['coreCaller']])
        d = PIOCoreRPC.call(args)
        d.addCallback(lambda _: new_project_dir)
        return d
