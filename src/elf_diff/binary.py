# -*- coding: utf-8 -*-

# -*- mode: python -*-
#
# elf_diff
#
# Copyright (C) 2019  Noseglasses (shinynoseglasses@gmail.com)
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, version 3.
#
# This program is distributed in the hope that it will be useful, but WITHOUT but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with along with
# this program. If not, see <http://www.gnu.org/licenses/>.
#

from elf_diff.error_handling import warning
from elf_diff.symbol import getSymbolType, Symbol
from elf_diff.settings import Settings

import re
import os
import subprocess  # nosec # silence bandid warning
from typing import Optional, List, Type, Dict, Tuple


SOURCE_CODE_START_TAG = "...ED_SOURCE_START..."
SOURCE_CODE_END_TAG = "...ED_SOURCE_END..."


def preHighlightSourceCode(src: str) -> str:
    """Tag the start and end of source code in order to allow it to be replaced by some sort of tag"""
    return "%s%s%s" % (SOURCE_CODE_START_TAG, src, SOURCE_CODE_END_TAG)


class SourceFile(object):
    def __init__(self, filename: str, id_: int):
        self.filename: str = filename
        self.id_: int = id_


class Mangling(object):
    def __init__(self, mangling_file: Optional[str]):
        """Init mangling class."""
        self.mangling_file: Optional[str] = mangling_file
        self.mangling: Optional[Dict[str, str]] = None

        self._setupMangling()

    def _setupMangling(self) -> None:
        """Setup the mangling by reading symbols from a mangling file"""
        if self.mangling_file is None:
            return
        if not os.path.isfile(self.mangling_file):
            return
        with open(self.mangling_file, "r") as f:
            lines: List[str] = f.read().splitlines()
            self.mangling = {}
            line_id: int = 0
            # Read line pairs, first line is mangled, second line is demangled symbol
            for line in lines:
                if line_id == 0:
                    mangled_symbol: str = line
                else:
                    demangled_symbol: str = line
                    self.mangling[mangled_symbol] = demangled_symbol
                line_id = (line_id + 1) % 2

            print(
                "Mangling info of "
                + str(len(self.mangling))
                + " symbols read from file '"
                + self.mangling_file
                + "'"
            )

    def demangle(self, symbol_name: str) -> Tuple[str, bool]:
        """Try to demangle a symbol"""
        if self.mangling is None:
            return symbol_name, False
        if symbol_name in self.mangling.keys():
            return self.mangling[symbol_name], True

        return symbol_name, False


class SymbolCollector(object):
    def __init__(self, binary):
        self.header_line_re = re.compile("^(0x)?[0-9A-Fa-f]+ <(.+)>:")
        self.instruction_line_re = re.compile(
            r"^\s*[0-9A-Fa-f]+:\s*((?:\s*[0-9a-fA-F]{2})+)\s+(.*)\s*"
        )
        self.cur_symbol: Optional[Symbol] = None
        self.symbols: List[Symbol] = []
        self.n_instruction_lines: int = 0
        self.binary = binary  # type: Binary

    def _submitSymbol(self) -> None:
        """Submit the most recent symbol that was collected"""
        if self.cur_symbol is not None:
            self.symbols.append(self.cur_symbol)
            self.cur_symbol = None

    def _checkSymbolHeaderLine(self, line: str) -> bool:
        """Check a line read from a file and process it if it is a symbol header line"""
        header_match = re.match(self.header_line_re, line)
        if header_match:
            if self.cur_symbol:
                self._submitSymbol()

            symbol_name_mangled: str = header_match.group(2)

            if symbol_name_mangled in self.binary.symbols.keys():
                self.cur_symbol = self.binary.symbols[symbol_name_mangled]
            return True

        return False

    @staticmethod
    def _unifyX86InstructionLine(line: str) -> str:
        # The x86 instruction 0xC3 is named retq for 64 bit and ret for 32 bit.
        # Strangely several versions of objdump, namely 2.34 and 2.36.1 output either 'ret' or 'retq'
        # both for x86_64 binaries.
        #
        # To make comparing files portable, replace the retq with ret.
        return re.sub(r"(^.*\sc3\s+)retq(.*)$", r"\1ret\2", line)

    def _unifyInstructionLine(self, line: str) -> str:
        """Fixup the assembly output by objdump in a way that it
        is the same for all versions of objdump
        """
        if self.binary.file_format == "elf64-x86-64":
            return SymbolCollector._unifyX86InstructionLine(line)
        return line

    def _gatherSymbolInstructions(self, objdump_output: str) -> None:
        """Gather the symbol instructions of a symbol"""
        for line in objdump_output.splitlines():
            unified_line = self._unifyInstructionLine(line)

            is_header_line: bool = self._checkSymbolHeaderLine(unified_line)
            if is_header_line:
                continue

            instruction_line_match = re.match(self.instruction_line_re, unified_line)
            if instruction_line_match:
                self.n_instruction_lines += 1
            if self.cur_symbol:
                if instruction_line_match:
                    instruction_line = instruction_line_match.group(2)
                    self.cur_symbol.addInstructions(instruction_line)
                    # print("Found instruction line \'%s\'" % (unified_instruction_line))
                else:
                    if (len(unified_line) > 0) and (not unified_line.isspace()):
                        self.cur_symbol.addInstructions(
                            preHighlightSourceCode(unified_line)
                        )

        if self.cur_symbol:
            self._submitSymbol()


class Binary(object):
    def __init__(
        self,
        settings: Settings,
        filename: str,
        symbol_selection_regex: Optional[str] = None,
        symbol_exclusion_regex: Optional[str] = None,
        mangling: Optional[Mangling] = None,
    ):
        """Init binary object."""
        self.settings: Settings = settings
        self.filename: str = filename
        self.symbol_type: Type[Symbol] = getSymbolType(settings.language)

        self.mangling: Optional[Mangling] = mangling
        self.binutils_work: bool = True

        self.text_size: int = 0
        self.data_size: int = 0
        self.bss_size: int = 0
        self.overall_size: int = 0
        self.progmem_size: int = 0
        self.static_ram_size: int = 0

        self.file_format: Optional[str] = None

        self.symbol_selection_regex: Optional[str] = symbol_selection_regex
        self.symbol_selection_regex_compiled = None
        if symbol_selection_regex is not None:
            self.symbol_selection_regex_compiled = re.compile(symbol_selection_regex)

        self.symbol_exclusion_regex: Optional[str] = symbol_exclusion_regex
        self.symbol_exclusion_regex_compiled = None
        if symbol_exclusion_regex is not None:
            self.symbol_exclusion_regex_compiled = re.compile(symbol_exclusion_regex)

        if not self.filename:
            raise Exception("No binary filename defined")

        if not os.path.isfile(self.filename):
            raise Exception(
                "Unable to find filename {filename}".format(filename=filename)
            )
        self.source_files: Dict[int, SourceFile] = {}
        self.symbols: Dict[str, Symbol] = {}
        self.num_symbols_dropped: int = 0

        self._determineBinaryFileFormat()
        self._parseSymbols()

    def _registerSourceFile(self, filename: str, id_: int) -> SourceFile:
        new_source_file = SourceFile(filename, id_)
        self.source_files[id_] = new_source_file
        return new_source_file

    def _readObjdumpDisassemblyOutput(self) -> str:
        """Read the output of the objdump command applied to the binary"""
        cmd: List[str] = [self.settings.objdump_command, "-drwS", self.filename]
        proc = subprocess.Popen(  # nosec # silence bandid warning
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )

        o, e = proc.communicate()  # pylint: disable=unused-variable

        output: str = o.decode("utf8")
        # error = e.decode('utf8')

        return output

    def _readObjdumpArchiveHeadersOutput(self) -> str:
        """Read the output of the objdump command applied to the binary"""
        cmd: List[str] = [self.settings.objdump_command, "-a", self.filename]
        proc = subprocess.Popen(  # nosec # silence bandid warning
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )

        o, e = proc.communicate()  # pylint: disable=unused-variable

        output: str = o.decode("utf8")
        # error = e.decode('utf8')

        return output

    def _readNMOutput(self, demangle: bool) -> str:
        """Read the output of the nm command applied to the binary"""

        if demangle:
            demangle_token = "-C"
        else:
            demangle_token = ""

        cmd: List[str] = [
            self.settings.nm_command,
            "--print-size",
            "--size-sort",
            "--radix=d",
            demangle_token,
            self.filename,
        ]
        proc = subprocess.Popen(  # nosec # silence bandid warning
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )  # nosec # silence bandid warning

        o, e = proc.communicate()  # pylint: disable=unused-variable

        output: str = o.decode("utf8")
        # error = e.decode('utf8')

        return output

    def _readReadelfOutput(self) -> str:
        """read the output of the readelf command appliead to the binary"""
        cmd: List[str] = [
            self.settings.readelf_command,
            "--debug-dump=info",
            self.filename,
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        o, e = proc.communicate()  # pylint: disable=unused-variable

        output: str = o.decode("utf8")
        # error = e.decode('utf8')

        return output

    def _readSizeOutput(self) -> str:
        """read the output of the size command appliead to the binary"""
        cmd: List[str] = [self.settings.size_command, self.filename]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        o, e = proc.communicate()  # pylint: disable=unused-variable

        output: str = o.decode("utf8")
        # error = e.decode('utf8')

        return output

    def _addSymbol(self, symbol: Symbol) -> None:
        """Add a new symbol to the collection of symbols associated with the binary"""
        symbol.init()

        self.symbols[symbol.name_mangled] = symbol

    def _isSymbolSelected(self, symbol_name: str) -> bool:
        """Check if a symbol is selected via a regex"""
        if self.symbol_exclusion_regex_compiled is not None:
            if re.match(self.symbol_exclusion_regex_compiled, symbol_name):
                return False

        if self.symbol_selection_regex_compiled is None:
            return True

        if re.match(self.symbol_selection_regex_compiled, symbol_name):
            return True

        return False

    def _determineSymbolSizes(self) -> None:
        """Determine the sizes of symbols"""
        size_output: str = self._readSizeOutput()

        size_re = re.compile(r"^\s*([0-9]+)\s+([0-9]+)\s+([0-9]+)\s+([0-9]+)")
        sizes_sucessfully_determined: bool = False
        for line in size_output.splitlines():
            size_match = re.match(size_re, line)
            if size_match:
                self.text_size = int(size_match.group(1))
                self.data_size = int(size_match.group(2))
                self.bss_size = int(size_match.group(3))
                self.overall_size = int(size_match.group(4))

                self.progmem_size = self.text_size + self.data_size
                self.static_ram_size = self.data_size + self.bss_size

                sizes_sucessfully_determined = True

                break

        if not sizes_sucessfully_determined:
            warning(
                "Unable to determine resource consumptions. Is the proper size utility used?"
            )
            self.binutils_work = False

    def _generateSymbol(
        self,
        symbol_name_mangled: str,
        symbol_name_possibly_demangled: str,
        symbol_name_is_demangled: bool,
    ) -> Optional[Symbol]:
        """Generate a symbol based on a symbol name but only if the symbol is intented to be selected."""
        if self._isSymbolSelected(symbol_name_possibly_demangled):
            # print("Considering symbol " + symbol_name_possibly_demangled)
            return self.symbol_type(
                symbol_name_mangled,
                symbol_name_possibly_demangled,
                symbol_name_is_demangled,
            )
        # print("Ignoring symbol " + symbol_name)
        self.num_symbols_dropped += 1
        return None

    def demangle(
        self, symbol_name_with_mangling_state_unknown: str
    ) -> Tuple[str, bool]:
        """Try to demangle a symbol name"""
        if self.mangling is not None:
            symbol_name_demangled: str
            was_demangled: bool
            symbol_name_demangled, was_demangled = self.mangling.demangle(
                symbol_name_with_mangling_state_unknown
            )

            if was_demangled:
                return symbol_name_demangled, True  # is demangled

        if self.binutils_work:
            return (
                symbol_name_with_mangling_state_unknown,
                True,
            )  # Binutils work, so we expect demangling having already taken place

        return (
            symbol_name_with_mangling_state_unknown,
            False,
        )  # Neither explicit demangling, nor binutils demangling worked

    def _determineBinaryFileFormat(self) -> None:
        """Get information about the architecture of the binary"""
        objdump_output: str = self._readObjdumpArchiveHeadersOutput()
        file_format_match = re.search(r"file format\s+(\S+)", objdump_output)
        if file_format_match:
            self.file_format = file_format_match.group(1)
            print("File format of binary %s: %s" % (self.filename, self.file_format))
        else:
            print("Unable to detect binary file format of %s" % self.filename)

    def _gatherSymbolInstructions(self) -> None:
        """Gather the instructions associated with a symbol"""
        objdump_output: str = self._readObjdumpDisassemblyOutput()

        symbol_collector = SymbolCollector(self)
        symbol_collector._gatherSymbolInstructions(objdump_output)

        for symbol in symbol_collector.symbols:
            self._addSymbol(symbol)

        self.instructions_available: bool = len(symbol_collector.symbols) > 0

        if symbol_collector.n_instruction_lines == 0:
            warning(f"Unable to read assembly from binary '{self.filename}'.")

    def _gatherSymbolProperties(self) -> None:
        """Gather the properties of a symbol"""
        nm_output_mangled: str = self._readNMOutput(demangle=False)
        nm_output_demangled: str = self._readNMOutput(demangle=True)

        self.num_symbols_dropped = 0
        nm_regex = re.compile(r"^[0-9A-Fa-f]+\s([0-9A-Fa-f]+)\s(\w)\s(.+)")
        for line_mangled, line_demangled in zip(
            nm_output_mangled.splitlines(), nm_output_demangled.splitlines()
        ):
            nm_match_mangled = re.match(nm_regex, line_mangled)
            nm_match_demangled = re.match(nm_regex, line_demangled)

            if nm_match_mangled:
                symbol_size_str: str = nm_match_mangled.group(1)
                symbol_type: str = nm_match_mangled.group(2)

                symbol_name_mangled: str = nm_match_mangled.group(3)
                symbol_name_with_mangling_state_unknown: str = nm_match_demangled.group(
                    3
                )

                symbol_name_possibly_demangled: str
                symbol_name_is_demangled: bool
                (
                    symbol_name_possibly_demangled,
                    symbol_name_is_demangled,
                ) = self.demangle(symbol_name_with_mangling_state_unknown)

                if symbol_name_mangled not in self.symbols.keys():
                    data_symbol: Optional[Symbol] = self._generateSymbol(
                        symbol_name_mangled,
                        symbol_name_possibly_demangled,
                        symbol_name_is_demangled,
                    )
                    if data_symbol is not None:
                        data_symbol.size = int(symbol_size_str)
                        data_symbol.type_ = symbol_type
                        self._addSymbol(data_symbol)
                else:
                    self.symbols[symbol_name_mangled].size = int(symbol_size_str)
                    self.symbols[symbol_name_mangled].type_ = symbol_type

    class _DebugInformationCollector(object):
        def __init__(self, binary):
            # type: (Binary) -> None
            self._binary: Binary = binary
            self._header_line_regex = re.compile(
                r"\s*<[0-9a-f]+>\s*<[0-9a-f]+>:\s+Abbrev Number:\s*(\d+)\s+\((\w+)\).*"
            )
            self._info_line_regex = re.compile(r"\s*<[0-9a-f]+>\s+(\S+)\s*:\s*(\S.*)")
            self._name_regex = re.compile(r".*\s+(\S+)")
            self._source_file_regex = re.compile(r".*\s+(\S+)")

            self._header_id: Optional[int] = None
            self._header_tag: Optional[str] = None

            self._name_mangled: Optional[str] = None
            self._source_id: Optional[id] = None
            self._source_line: Optional[int] = None
            self._source_column: Optional[int] = None

        def _flushSymbolInfo(self) -> None:
            if self._name_mangled is not None:
                if self._name_mangled in self._binary.symbols.keys():
                    symbol = self._binary.symbols[self._name_mangled]

                    symbol.source_id = self._source_id
                    symbol.source_line = self._source_line
                    symbol.source_column = self._source_column

            self._name_mangled = None
            self._source_id = None
            self._source_line = None
            self._source_column = None

        def parseReadelfOutput(self, readelf_output) -> None:
            for line in readelf_output.splitlines():
                header_line_match = re.match(self._header_line_regex, line)
                if header_line_match:
                    # print("Header line: %s" % line)
                    self._header_id = header_line_match.group(1)
                    self._header_tag = header_line_match.group(2)
                    self._flushSymbolInfo()
                    continue

                info_line_match = re.match(self._info_line_regex, line)
                if info_line_match:
                    tag: str = info_line_match.group(1)
                    add_info: str = info_line_match.group(2)

                    if tag == "DW_AT_linkage_name":
                        name_match = re.match(self._name_regex, add_info)
                        if name_match is None:
                            raise Exception("Undeciferable info line '%s'" % line)
                        self._name_mangled = name_match.group(1)
                    elif tag == "DW_AT_decl_file":
                        self._source_id = int(add_info)
                    elif tag == "DW_AT_decl_line":
                        self._source_line = int(add_info)
                    elif tag == "DW_AT_decl_column":
                        self._source_column = int(add_info)
                    elif tag == "DW_AT_name":
                        if self._header_tag == "DW_TAG_compile_unit":
                            source_file_match = re.match(
                                self._source_file_regex, add_info
                            )
                            if source_file_match is None:
                                raise Exception(
                                    "Unable to determine source filename from dwarf output"
                                )
                            source_filename = source_file_match.group(1)
                            source_id = self._header_id
                            self._binary._registerSourceFile(source_filename, source_id)

            # There might be a last ungoing data set being parsed
            self._flushSymbolInfo()

    def _gatherDebugInformation(self) -> None:
        readelf_output = self._readReadelfOutput()
        # print(readelf_output)

        info_collector = Binary._DebugInformationCollector(self)
        info_collector.parseReadelfOutput(readelf_output)

    def _initSymbols(self) -> None:
        for symbol_name_mangled in sorted(self.symbols.keys()):
            symbol = self.symbols[symbol_name_mangled]
            symbol.init()

    def _parseSymbols(self) -> None:
        """Parse symbols from the binary"""
        self._determineSymbolSizes()
        self._gatherSymbolProperties()
        self._gatherSymbolInstructions()
        self._gatherDebugInformation()
        self._initSymbols()
