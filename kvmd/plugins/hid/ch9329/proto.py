# ========================================================================== #
#                                                                            #
#    KVMD - The main PiKVM daemon.                                           #
#                                                                            #
#    Copyright (C) 2018-2021  Maxim Devaev <mdevaev@gmail.com>               #
#                                                                            #
#    This program is free software: you can redistribute it and/or modify    #
#    it under the terms of the GNU General Public License as published by    #
#    the Free Software Foundation, either version 3 of the License, or       #
#    (at your option) any later version.                                     #
#                                                                            #
#    This program is distributed in the hope that it will be useful,         #
#    but WITHOUT ANY WARRANTY; without even the implied warranty of          #
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the           #
#    GNU General Public License for more details.                            #
#                                                                            #
#    You should have received a copy of the GNU General Public License       #
#    along with this program.  If not, see <https://www.gnu.org/licenses/>.  #
#                                                                            #
# ========================================================================== #
#
# KVMD CH9329 Plugin - CH9329 HID plugin for KVMD based on serial plugin
# Copyright (C) 2021-2077 xe5700
# https://github.com/xe5700
#
#

import dataclasses
import math
import struct

from ....keyboard.mappings import KEYMAP
from ....mouse import MouseRange

debug = True

# CH9329 命令列表 Commands list
# 头部信息 header information
CH9329_HEADER = [0x57, 0xAB, 0x00]
# 获取信息 get information
GET_INFO = bytes(CH9329_HEADER + [0x01, 0x00])

# 发送USB键盘普通数据 send usb keyboard normal information
SEND_KB_GENERAL_DATA = bytes(CH9329_HEADER + [0x02, 0x08])
# 发送USB键盘多媒体数据 send usb keyboard multimedia information
SEND_KB_MEDIA_DATA = bytes(CH9329_HEADER + [0x03])
# 发送USB鼠标绝对数据 send usb mouse abstract postion.
SEND_MS_ABS_DATA = bytes(CH9329_HEADER + [0x04, 0x07, 0x02])
# 发送USB鼠标相对数据 send usb mouse relative postion.
SEND_MS_REL_DATA = bytes(CH9329_HEADER + [0x05, 0x05, 0x01])
# 发送自定义HID数据 send custom hid information.
SEND_MY_HID_DATA = bytes(CH9329_HEADER + [0x06])
# 获取自定义HID数据 get custom hid information.
READ_MY_HID_DATA = bytes(CH9329_HEADER + [0x87])
# 获取配置 get configuration
GET_PARA_CFG = bytes(CH9329_HEADER + [0x08])
# 获取字符串描述符配置 get usb string configuration
GET_USB_STRING = bytes(CH9329_HEADER + [0x0A])
# 设置字符串描述符配置 set usb string configuration
SET_USB_STRING = bytes(CH9329_HEADER + [0x0B])
# 恢复出厂默认配置 reset to default configuration
SET_DEFAULT_CFG = bytes(CH9329_HEADER + [0x0C])
# 复位芯片 reset ch9329
RESET = bytes(CH9329_HEADER + [0x0F, 0x00])


# =====
class BaseEvent:
    #    def make_request(self) -> bytes:
    #        raise NotImplementedError

    def make_down(self) -> bytes:
        raise NotImplementedError

    #   def make_up(self) -> bytes:
    #        raise NotImplementedError


@dataclasses.dataclass(frozen=True)
class KeyEvent(BaseEvent):
    name: str
    state: bool

    def __post_init__(self) -> None:
        assert self.name in KEYMAP

    def make_down(self) -> bytes:
        if not self.state:
            return release_keyboard
        code = KEYMAP[self.name].otg.code
        if debug: print(f"按下键盘 {self.name}")
        return make_with_sum(SEND_KB_GENERAL_DATA + struct.pack("2x1B5x", code))


@dataclasses.dataclass()
class StatusEvent:
    state: bool


class SetKeyboardOutputEvent(StatusEvent):
    pass


class SetMouseOutputEvent(StatusEvent):
    pass


mouse_btn_map = {
    "left": (0b00000001, True),
    "right": (0b00000010, True),
    "middle": (0b00000100, True),
    "up": (0x01, False),
    "down": (0x81, False)

}


@dataclasses.dataclass(frozen=True)
class MouseButtonEvent(BaseEvent):
    name: str
    state: bool

    def __post_init__(self) -> None:
        assert self.name in ["left", "right", "middle", "up", "down"]

    def make_down(self) -> bytes:
        if not self.state:
            return release_mouse
        code, isNormalKey = mouse_btn_map[self.name]
        nKey = code if isNormalKey else 0x00
        moveKey = 0x00 if isNormalKey else code
        if debug:
            print(f"按下鼠标 {self.name}")
        return make_with_sum(SEND_MS_REL_DATA + struct.pack("B2xB", nKey, moveKey))


@dataclasses.dataclass()
class MouseMoveEvent(BaseEvent):
    to_x: int
    to_y: int
    fixed_x: int = dataclasses.field(default=0)
    fixed_y: int = dataclasses.field(default=0)

    def __post_init__(self) -> None:
        assert MouseRange.MIN <= self.to_x <= MouseRange.MAX
        assert MouseRange.MIN <= self.to_y <= MouseRange.MAX
        # CH9329 resoulstion only 4096x4096
        # try to fixed it
        self.fixed_x = self.to_x + MouseRange.MAX
        self.fixed_y = self.to_y + MouseRange.MAX
        self.fixed_x /= (MouseRange.MAX * 2 / 4096)
        self.fixed_y /= (MouseRange.MAX * 2 / 4096)
        self.fixed_x = int(math.floor(self.fixed_x))
        self.fixed_y = int(math.floor(self.fixed_y))

    def make_down(self) -> bytes:
        if debug: print(f"移动鼠标 {self.fixed_x} {self.fixed_y}")
        rep = make_with_sum(SEND_MS_ABS_DATA + struct.pack("<xhhx", self.fixed_x, self.fixed_y))
        if debug: print(f"鼠标代码 {rep.hex(' ')}")
        return rep


@dataclasses.dataclass(frozen=False)
class MouseRelativeEvent(BaseEvent):
    delta_x: int
    delta_y: int
    fixed_x: int = dataclasses.field(default=0)
    fixed_y: int = dataclasses.field(default=0)

    def __post_init__(self) -> None:
        assert -127 <= self.delta_x <= 127
        assert -127 <= self.delta_y <= 127
        self.fixed_x = self.delta_x + 127
        self.fixed_y = self.delta_y + 127

    def make_down(self) -> bytes:
        if debug: print(f"相对移动鼠标 {self.delta_x} {self.delta_y}")
        return make_with_sum(SEND_MS_REL_DATA + struct.pack("x2Bx", self.delta_x, self.delta_y))


@dataclasses.dataclass(frozen=False)
class MouseWheelEvent(BaseEvent):
    delta_x: int
    delta_y: int
    fixed_x: int = dataclasses.field(default=0)
    fixed_y: int = dataclasses.field(default=0)

    def __post_init__(self) -> None:
        assert -127 <= self.delta_x <= 127
        assert -127 <= self.delta_y <= 127
        self.fixed_x = math.floor((self.delta_x + 127) / 2)
        self.fixed_y = math.floor((self.delta_y + 127) / 2)

    def make_down(self) -> bytes:
        if debug:
            print(f"滚动 {self.fixed_y}")
        return make_with_sum(SEND_MS_REL_DATA + struct.pack("<3xB", self.fixed_y))


def make_with_sum(data: bytes) -> bytes:
    sum = 0
    for i in data:
        sum += i
    sum = sum.to_bytes(4, 'little')
    return data + bytes([sum[0]])


def check_with_sum(data: bytes) -> bool:
    _l = len(data)
    _data_sum = data[_l - 1]
    _compute_sum = 0
    for i in range(0, _l - 1):
        _compute_sum += data[i]
    _compute_sum = _compute_sum.to_bytes(4, 'little')[0]
    return _data_sum == _compute_sum


release_keyboard = make_with_sum(SEND_KB_GENERAL_DATA + struct.pack("8x"))
release_mouse = make_with_sum(SEND_MS_REL_DATA + struct.pack("4x"))

GET_INFO_S = make_with_sum(GET_INFO)
RESET_S = make_with_sum(RESET)

CH9329_HEADER_S = bytes(CH9329_HEADER)
ERRORS_REP_CODES = {
    0x00: "DEF_CMD_SUCCESS",
    0xE1: "DEF_CMD_ERR_TIMEOUT",
    0XE2: "DEF_CMD_ERR_HEAD",
    0xE3: "DEF_CMD_ERR_CMD",
    0xE4: "DEF_CMD_ERR_SUM",
    0xE5: "DEF_CMD_ERR_PARA",
    0xE6: "DEF_CMD_ERR_OPERATE"
}
