# Copyright (C) 2015 Gerrit Addiks <gerrit@addiks.net>
# https://github.com/addiks/gedit-dbgp-plugin
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

from gi.repository import GLib, Gtk
from _thread import start_new_thread
from gladehandler import GladeHandler
# from helpers import *
import socket
import base64
import time
import os
import xml.etree.ElementTree as ElementTree
addiksdbgp = __import__("addiks-dbgp")


class DebugSession:
    def __init__(self, plugin, clientSocket):
        self._plugin = plugin
        self._client_socket = clientSocket
        self._is_waiting_for_server = False
        self._glade_builder = None
        self._glade_handler = None
        self._options = {
            'fileuri':          None,
            'language':         None,
            'protocol_version': None,
            'appid':            None,
            'idekey':           None,
            'engine':           None,
            'author':           None,
            'url':              None,
            'copyright':        None,
        }
        self._features = {
            'language_supports_threads': None,
            'language_name':             None,
            'language_version':          None,
            'encoding':                  None,
            'protocol_version':          None,
            'supports_async':            None,
            'data_encoding':             None,
            'breakpoint_languages':      None,
            'breakpoint_types':          None,
            'multiple_sessions':         None,
            'max_children':              None,
            'max_data':                  None,
            'max_depth':                 None,
            'extended_properties':       None,
        }
        self._types = []
        self._status = 'starting'
        self._transaction_id_counter = 1
        self._custom_watches = []
        self._expanded_watches = []
        self._path_mapping = None
        self._prepared_stack = []

    def init(self):

        # <?xml version="1.0" encoding="iso-8859-1"?>
        # <init xmlns="urn:debugger_protocol_v1"
        #      xmlns:xdebug="http://xdebug.org/dbgp/xdebug"
        #      fileuri="file:///usr/workspace/api.brille24.de/web/app.php"
        #      language="PHP"
        #      protocol_verion="1.0"
        #      appid="22855"
        #      idekey="TEST">
        #  <engine version="2.2.3"><![CDATA[Xdebug]]></engine>
        #  <author><![CDATA[Derick Rethans]]></author>
        #  <url><![CDATA[http://xdebug.org]]></url>
        #  <copyright><![CDATA[Copyright (c) 2002-2013 by Derick Rethans]]></copyright>
        # </init>

        initXml = self.__read_xml_packet()

        self._options.update(initXml.attrib)

        profileManager = self._plugin.get_profile_manager()

        self._path_mapping = None
        for profileName in profileManager.get_profiles():
            profile = profileManager.get_profile(profileName)
            if profile.get('dbgp_ide_key') == initXml.attrib['idekey']:
                self._path_mapping = profileManager.get_pathmapping_manager(profileName)
                break

        for childXml in initXml:
            self._options[childXml.tag] = childXml.text

        for feature_name in self._features:
            if self._features[feature_name] is not None:
                self.__send_command(
                    "feature_set",
                    ['-n '+feature_name, '-v '+str(self._features[feature_name])]
                )

        feature_names = self._features.keys()
        for feature_name in feature_names:
            featureXml = self.__send_command("feature_get", ['-n '+feature_name])
            if featureXml.attrib['supported'] == '1':
                self._features[feature_name] = featureXml.text
            else:
                self._features[feature_name] = None

        self._update_typemap()

        breakpoints = addiksdbgp.AddiksDBGPApp.get().get_all_breakpoints()
        for filePath in breakpoints:
            for line in breakpoints[filePath]:
                self.set_breakpoint({
                    'type':     'line',
                    'filename': filePath,
                    'lineno':   line,
                })

        GLib.idle_add(self.__show_window)

    def _update_typemap(self):
        typesXml = self.__send_command("typemap_get")
        for mapXml in typesXml:
            xsiType = None
            if 'xsi:type' in mapXml.attrib:
                xsiType = mapXml.attrib['xsi:type']
            self._types.append([mapXml.attrib['name'], mapXml.attrib['type'], xsiType])

    def __show_window(self):
        builder = self._getGladeBuilder()
        window = builder.get_object("windowSession")
        window.set_title("Running process: " + self._options['idekey'])
        #   window.set_keep_above(True)
        # accelGroup = addiksdbgp.AddiksDBGPApp.get().get_all_windows()[0].get_accel_group()
        # window.add_accel_group(accelGroup)
        window.show_all()
        start_new_thread(self.__after_show_window, ())

    def __after_show_window(self):
        if not self.is_in_breakpoint():
            self.run()
        self.__update_view(True)

    def close(self):
        GLib.idle_add(self.__close)

    def __close(self):
        builder = self._getGladeBuilder()
        window = builder.get_object("windowSession")
        window.hide()

    def is_in_breakpoint(self):
        full_stack = self.get_stack()
        if not full_stack:
            return False
        stack = full_stack[-1]
        if self._path_mapping is not None:
            stack['filename'] = self._path_mapping.mapRemoteToLocal(stack['filename'])
        if stack['filename'][0:7] == 'file://':
            stack['filename'] = stack['filename'][7:]
        elif stack['filename'][0:5] == 'file:':
            stack['filename'] = stack['filename'][5:]
        breakpoints = addiksdbgp.AddiksDBGPApp.get().get_all_breakpoints()
        for filePath in breakpoints:
            for line in breakpoints[filePath]:
                if stack['filename'] == filePath and int(stack["lineno"]) == line:
                    return True
        return False

    def mapRemoteToLocalPath(self, remotePath):
        if self._path_mapping is not None:
            return self._path_mapping.mapRemoteToLocal(remotePath)
        return remotePath

    def add_watch(self, definition):
        if definition not in self._custom_watches:
            self._custom_watches.append(definition)
            self.__update_view()

    def remove_watch(self, definition):
        self._custom_watches.remove(definition)
        self.__update_view()

    def clear_watches(self):
        self._custom_watches = []
        self._expanded_watches = []
        self.__update_view()

    def get_watches(self):
        return self._custom_watches

    def get_types(self):
        return self._types

    def expand_watch(self, fullName):
        if fullName not in self._expanded_watches:
            self._expanded_watches.append(fullName)
            self.__update_view()

    def collapse_watch(self, fullName):
        if fullName in self._expanded_watches:
            self._expanded_watches.remove(fullName)
            self.__update_view()

    def __hideWindow(self):
        builder = self._getGladeBuilder()
        window = builder.get_object("windowSession")
        window.hide()

    # ## COMMANDS

    def run(self, clearBreakpoints=False):
        try:
            if clearBreakpoints:
                breakpoints = self.list_breakpoints()
                for breakpointId in breakpoints:
                    self.remove_breakpoint(breakpointId)
            responseXml = self.__send_command("run")
            self._status = responseXml.attrib['status']
            self.__update_view(True)
            if self._status == "stopping":
                self.stop()
        except BrokenPipeError:
            GLib.idle_add(self.__hideWindow)
            addiksdbgp.AddiksDBGPApp.get().remove_session(self)

    def step_into(self):
        try:
            responseXml = self.__send_command("step_into")
            self._status = responseXml.attrib['status']
            self.__update_view(True)
            if self._status == "stopping":
                self.stop()
        except BrokenPipeError:
            GLib.idle_add(self.__hideWindow)
            addiksdbgp.AddiksDBGPApp.get().remove_session(self)

    def step_over(self):
        try:
            responseXml = self.__send_command("step_over")
            self._status = responseXml.attrib['status']
            self.__update_view(True)
            if self._status == "stopping":
                self.stop()
        except BrokenPipeError:
            GLib.idle_add(self.__hideWindow)
            addiksdbgp.AddiksDBGPApp.get().remove_session(self)

    def step_out(self):
        try:
            responseXml = self.__send_command("step_out")
            self._status = responseXml.attrib['status']
            self.__update_view(True)
            if self._status == "stopping":
                self.stop()
        except BrokenPipeError:
            GLib.idle_add(self.__hideWindow)
            addiksdbgp.AddiksDBGPApp.get().remove_session(self)

    def stop(self):
        try:
            responseXml = self.__send_command("stop")
            self._status = responseXml.attrib['status']
            self._client_socket.close()
            self.__update_view()
            GLib.idle_add(self.__hideWindow)
            addiksdbgp.AddiksDBGPApp.get().remove_session(self)
        except BrokenPipeError:
            GLib.idle_add(self.__hideWindow)
            addiksdbgp.AddiksDBGPApp.get().remove_session(self)

    def set_breakpoint(self, input_options={}):
        arguments, expression = self.__get_breakpoint_arguments(input_options)
        self.__send_command("breakpoint_set", arguments, expression)

    def list_breakpoints(self):
        responseXml = self.__send_command("breakpoint_list")
        breakpoints = {}
        for breakpointXml in responseXml:
            options = breakpointXml.attrib
            if breakpointXml:
                options['expression'] = breakpointXml[0].text
            else:
                options['expression'] = None
            breakpoints[options['id']] = options
        return breakpoints

    def get_breakpoint(self, breakpoint_id):
        responseXml = self.__send_command("breakpoint_get", ['-d '+breakpoint_id])
        breakpoints = {}
        for breakpointXml in responseXml:
            options = breakpointXml.attrib
            options['expression'] = breakpointXml[0].text
            breakpoints[options['id']] = options
        return breakpoints[0]

    def remove_breakpoint_by_file_line(self, filePath, line):
        breakpoints = self.list_breakpoints()
        if self._path_mapping is not None:
            filePath = self._path_mapping.mapLocalToRemote(filePath)
        for breakpointId in breakpoints:
            breakpoint = breakpoints[breakpointId]
            if breakpoint['filename'] == "file://"+filePath and int(breakpoint['lineno']) == line:
                self.remove_breakpoint(breakpointId)

    def remove_breakpoint(self, breakpoint_id):
        self.__send_command("breakpoint_remove", ['-d '+breakpoint_id])

    def update_breakpoint(self, breakpoint_id, input_options={}):
        arguments, expression = self.__get_breakpoint_arguments(input_options)
        arguments.append("-d "+breakpoint_id)
        self.__send_command("breakpoint_update", arguments, expression)

    def get_property(self, fullName):
        responseXml = self.__send_command("property_get", ['-n '+fullName])
        if responseXml:
            return responseXml[0]

    def set_property(self, fullName, typeName, newValue):
        self.__send_command(
            "property_set",
            ['-n '+fullName, '-t '+typeName, '-l {{#DATALENGTH#}}'],
            newValue
        )
        self.__update_view()

    def get_max_stack_depth(self):
        responseXml = self.__send_command("stack-depth")
        return responseXml.attrib['depth']

    def get_stack(self, depth=None, glib_idle_add=None):
        # see: http://xdebug.org/docs-dbgp.php#id50
        arguments = []
        if depth is not None:
            arguments.append("-d "+depth)
        stack = []
        if self._status in ['running', 'break']:
            responseXml = self.__send_command("stack_get")
            for stackXml in responseXml:
                if stackXml.tag == "error":
                    break
                stack.append(stackXml.attrib)
            if stack and 'level' in stack[0]:
                stack.sort(key=lambda entry: entry['level'], reverse=True)
        if glib_idle_add is not None:
            GLib.idle_add(glib_idle_add, stack)
        return stack

    def get_context_names(self, depth=None):
        arguments = []
        if depth is not None:
            arguments.append("-d "+depth)
        names = {}
        if self._status in ['running', 'break']:
            responseXml = self.__send_command("context_names", arguments)
            for contextXml in responseXml:
                names[contextXml.attrib['name']] = contextXml.attrib['id']
        return names

    def get_context(self, context_name_id, depth=None):
        # see: http://xdebug.org/docs-dbgp.php#id53
        responseXml = None
        if self._status in ['running', 'break']:
            arguments = ["-c "+context_name_id]
            if depth is not None:
                arguments.append("-d "+depth)

            responseXml = self.__send_command("context_get", arguments)
        return responseXml

    def eval_expression(self, expression):
        return self.__send_command("eval", [], expression)

    # ## HELPERS

    def get_prepared_stack(self):
        return self._prepared_stack

    def __update_view(self, openTopFile=False):

        try:

            # ## CLEANUP

            userInterface = self._getGladeHandler()
            scroll = userInterface.getWatchesScrollPosition()
            userInterface.clearStack()
            userInterface.clearWatches()

            if self._status in ['stopping', 'stopped']:
                self._prepared_stack = []
            else:
                self._prepared_stack = self.get_stack()

            for view in addiksdbgp.AddiksDBGPApp.get().get_all_views():
                GLib.idle_add(view.update_stack_marks)

            if self._status in ['stopping', 'stopped']:
                return

            # ## STACK-TRACE

            topStackFilepath = None
            topStackLineNr = 0
            for stack in self._prepared_stack:

                if self._path_mapping is not None:
                    stack['filename'] = self._path_mapping.mapRemoteToLocal(stack['filename'])

                if int(stack['level']) == 0:
                    topStackFilepath = stack['filename']
                    topStackLineNr = int(stack["lineno"])

                where = ""
                if "where" in stack:
                    where = stack["where"]

                line = stack["lineno"]

                filepath = stack["filename"]

                userInterface.addStackRow(filepath, line, where)

            if openTopFile and topStackFilepath is not None:
                GLib.idle_add(self.open_uri_resouce, topStackFilepath, topStackLineNr)

            # ## WATCHES

            expandFullNames = []

            for definition in self._custom_watches:
                responseXml = self.eval_expression(definition)
                if len(responseXml) > 1:
                    userInterface.addWatchRow(definition, definition, "array")
                    index = 0
                    for propertyXml in responseXml:
                        fullName, name = self.__readXmlElementNames(propertyXml)

                        if fullName in self._expanded_watches:
                            expandFullNames.append(fullName)
                            propertyXml = self.get_property(fullName)

                        fullName = definition+"["+str(index)+"]"
                        userInterface.addWatchRow(fullName, str(index), "array")
                        userInterface.setWatchRowValue(
                            fullName,
                            self.__get_value_by_propertyXml(
                                propertyXml,
                                fullName,
                                expandFullNames
                            )
                        )

                        index += 1

                elif len(responseXml) == 1:
                    propertyXml = responseXml[0]
                    userInterface.addWatchRow(definition, definition)
                    userInterface.setWatchRowValue(
                        definition,
                        self.__get_value_by_propertyXml(
                            propertyXml,
                            definition,
                            expandFullNames
                        )
                    )

            writtenFullNames = []
            contextNames = self.get_context_names()
            for contextName in contextNames:
                contextNameId = contextNames[contextName]
                contextXml = self.get_context(contextNameId)

                for propertyXml in contextXml:
                    fullName, name = self.__readXmlElementNames(propertyXml)

                    if fullName is not None and fullName not in writtenFullNames:

                        if fullName in self._expanded_watches:
                            expandFullNames.append(fullName)
                            propertyXml = self.get_property(fullName)

                        userInterface.addWatchRow(fullName, name)
                        userInterface.setWatchRowValue(
                            fullName,
                            self.__get_value_by_propertyXml(
                                propertyXml,
                                fullName,
                                expandFullNames
                            )
                        )
                        writtenFullNames.append(fullName)

            for fullName in expandFullNames:
                userInterface.expandWatchRow(fullName)

            userInterface.setWatchesScrollPosition(scroll)

        except BrokenPipeError:
            GLib.idle_add(self.__hideWindow)
            addiksdbgp.AddiksDBGPApp.get().remove_session(self)

    def __get_value_by_propertyXml(
        self,
        propertyXml,
        parentFullName,
        expandFullNames=[],
        tryTypemapUpdate=True
    ):
        userInterface = self._getGladeHandler()

        tagName = propertyXml.tag

        if "}" in tagName:
            tagName = tagName.split('}', 1)[1]

        if tagName == "error":
            return propertyXml[0].text

        dataType = propertyXml.attrib['type']
        originalDataType = dataType

        for typeName, dbgpType, xsiType in self._types:
            if dataType == typeName:
                dataType = dbgpType

        if dataType == 'uninitialized':
            return "{uninitialized}"

        elif dataType == 'object':
            if propertyXml:
                for childPropertyXml in propertyXml:

                    fullName, name = self.__readXmlElementNames(childPropertyXml)

                    if fullName in self._expanded_watches:
                        expandFullNames.append(fullName)
                        childPropertyXml = self.get_property(fullName)

                    userInterface.addWatchRow(fullName, name, None, parentFullName)
                    userInterface.setWatchRowValue(
                        fullName,
                        self.__get_value_by_propertyXml(
                            childPropertyXml,
                            fullName,
                            expandFullNames
                        )
                    )
            else:
                userInterface.addWatchRow(None, None, None, parentFullName)
            return "object({numchildren}) : {classname}".format(
                **propertyXml.attrib
            )

        elif dataType == 'array':  # like a list
            return "{array is unimplemented type}"

        elif dataType == 'hash':  # like a dictionary
            if propertyXml:
                contentFound = False
                for childPropertyXml in propertyXml:
                    if childPropertyXml.tag == "{urn:debugger_protocol_v1}property":
                        contentFound = True
                        fullName, name = self.__readXmlElementNames(childPropertyXml)

                        if fullName in self._expanded_watches:
                            expandFullNames.append(fullName)
                            childPropertyXml = self.get_property(fullName)

                        userInterface.addWatchRow(fullName, name, None, parentFullName)
                        userInterface.setWatchRowValue(
                            fullName,
                            self.__get_value_by_propertyXml(
                                childPropertyXml,
                                fullName,
                                expandFullNames
                            )
                        )
                if not contentFound:
                    return self.__readXmlElementContent(childPropertyXml)
            else:
                userInterface.addWatchRow(None, None, None, parentFullName)
            return originalDataType + "(" + propertyXml.attrib['numchildren'] + ")"

        elif dataType in ['string', 'float', 'int']:
            return self.__readXmlElementContent(propertyXml)

        elif dataType in ['bool']:
            if propertyXml.text == '1':
                return 'true'
            else:
                return 'false'

        elif dataType in ['resource', 'null']:
            return "{"+originalDataType+"}"

        if tryTypemapUpdate:
            self._update_typemap()
            return self.__get_value_by_propertyXml(
                propertyXml,
                parentFullName,
                expandFullNames,
                False
            )

        content = self.__readXmlElementContent(propertyXml)
        if type(content) == str:
            return content

        return "{unknown type: '"+propertyXml.attrib['type']+"'}"

    def __readXmlElementContent(self, contentXml):
        for valueXml in contentXml.findall('{urn:debugger_protocol_v1}value'):
            return self.__readXmlElementContent(valueXml)
        content = str(contentXml.text)
        if 'encoding' in contentXml.attrib:
            if contentXml.attrib['encoding'] == "base64":
                content = base64.b64decode(content)
                if len(content) <= 0:
                    content = ""
                elif type(content) == bytes:
                    try:
                        content = content.decode("utf-8")
                    except UnicodeDecodeError:
                        content = "{charset-decoding-error while reading value}"
        return content

    def __readXmlElementNames(self, propertyXml):
        name = None
        if "name" in propertyXml.attrib:
            name = propertyXml.attrib["name"]
        else:
            for nameXml in propertyXml.findall("{urn:debugger_protocol_v1}name"):
                name = self.__readXmlElementContent(nameXml)

        fullName = None
        if "fullname" in propertyXml.attrib:
            fullName = propertyXml.attrib["fullname"]
        else:
            for nameXml in propertyXml.findall("{urn:debugger_protocol_v1}fullname"):
                fullName = self.__readXmlElementContent(nameXml)

        if fullName is None:
            fullName = name

        return fullName, name

    def open_uri_resouce(self, uri, line=None):
        if uri[0:5] == 'file:':
            if uri[5:7] == '//':
                filePath = uri[7:]
                if self._path_mapping is not None:
                    filePath = self._path_mapping.mapRemoteToLocal(filePath)
            else:
                filePath = uri[5:]
            addiksdbgp.AddiksDBGPApp.get().open_window_file(filePath, line)

    def __get_breakpoint_arguments(self, input_options={}):
        options = {
            'type':          "line",  # line, call, return, exception, conditional, watch
            'filename':      "",
            'lineno':        1,
            'state':         "enabled",
            'function':      "",  # function name for call or return
            'temporary':     "0",
            'hit_value':     "0",
            'hit_condition': "",
            'exception':     "",
            'expression':    "",
        }
        options.update(input_options)

        if options['type'] not in ['line', 'call', 'return', 'exception', 'condition', 'watch']:
            raise Exception("Invalid breakpoint type '"+options['type']+"'!")

        if len(options['filename']) > 1:
            if self._path_mapping is not None:
                options['filename'] = self._path_mapping.mapLocalToRemote(options['filename'])
            if options['filename'][0:7] != "file://":
                options['filename'] = "file://" + options['filename']

        arguments = [
            '-t ' + options['type'],
        ]

        expression = None

        if options['state'] != 'enabled':
            arguments.append('-s ' + options['state'])

        if options['type'] in ['line', 'condition']:
            arguments.append('-f ' + options['filename'])

        if options['type'] == 'line':
            arguments.append('-n ' + str(options['lineno']))

        if options['type'] in ['call', 'return']:
            arguments.append('-m ' + options['function'])

        if options['type'] in ['exception']:
            arguments.append('-x ' + options['exception'])

        if options['type'] in ['conditional', 'watch']:
            expression = options['expression']

        if options['hit_value'] != '0':
            arguments.append('-h ' + options['hit_value'])
            arguments.append('-o ' + options['hit_condition'])

        if options['temporary'] != '0':
            arguments.append('-r ' + options['temporary'])

        return [arguments, expression]

    def __send_command(self, command, arguments=[], data=None):
        clientSocket = self._client_socket
        transactionId = self._transaction_id_counter
        self._transaction_id_counter += 1
        argumentsString = ""
        if arguments:
            argumentsString = " "+(" ".join(arguments))
        if command not in ["breakpoint_set"] and False:
            dataString = " -- "
        else:
            dataString = ""
        if data is not None:
            dataString = " -- " + base64.b64encode(data.encode("utf-8")).decode("utf-8")
        argumentsString = argumentsString.replace("{{#DATALENGTH#}}", str(len(dataString)-4))
        packet = command+" -i "+str(transactionId)+argumentsString+dataString+"\0"
        # print(">>> "+packet)

        # there is already a command being executed,
        # wait until it is finished
        while self._is_waiting_for_server:
            time.sleep(1)

        self._is_waiting_for_server = True
        clientSocket.send(bytes(packet, 'UTF-8'))
        xml = self.__read_xml_packet(transactionId)
        self._is_waiting_for_server = False
        return xml

    def __read_xml_packet(self, transactionId=None):
        clientSocket = self._client_socket

        packetBegin = clientSocket.recv(128).decode("utf-8")

        if not packetBegin:
            raise socket.Error("Connection was closed")

        lengthString, xmlData = packetBegin.split('\0', 1)

        pendingDataSize = int(lengthString) - len(xmlData)
        while pendingDataSize > 0:
            dataBlock = clientSocket.recv(pendingDataSize).decode("utf-8")
            pendingDataSize -= len(dataBlock)
            xmlData += dataBlock

        clientSocket.recv(1)

        xmlData = xmlData.replace("\\n", "\n")
        xmlData = xmlData.replace("\\x00", "")
        xmlData = xmlData.replace("\0", "")

        if xmlData[-1] == "'":
            xmlData = xmlData[0:-1]

        # print("<<< ("+lengthString+"):"+xmlData+"\n")

        root = ElementTree.fromstring(xmlData)

        # make sure response and request are for the same transaction
        if transactionId is not None and 'transaction_id' in root.attrib:
            if transactionId != int(root.attrib['transaction_id']):
                # print("+++ Skipped packet because wrong transaction_id\n")
                root = self.__read_xml_packet(transactionId)

        if "status" in root.attrib:
            self._status = root.attrib['status']

        return root

    def _getGladeHandler(self):
        if self._glade_handler is None:
            self.__initGlade()
        return self._glade_handler

    def _getGladeBuilder(self):
        if self._glade_builder is None:
            self.__initGlade()
        return self._glade_builder

    def __initGlade(self):
        self._glade_builder = Gtk.Builder()
        self._glade_builder.add_from_file(os.path.dirname(__file__)+"/debugger.glade")
        self._glade_handler = GladeHandler(self._plugin, self._glade_builder, session=self)
        self._glade_builder.connect_signals(self._glade_handler)
