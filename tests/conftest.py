import sys
from unittest.mock import MagicMock

Gst = MagicMock(name='Gst')
Gst.MessageType.ERROR = 'GST_MESSAGE_ERROR'
Gst.MessageType.WARNING = 'GST_MESSAGE_WARNING'
Gst.MessageType.STATE_CHANGED = 'GST_MESSAGE_STATE_CHANGED'
_playing = MagicMock(name='PLAYING')
_playing.value_nick = 'playing'
Gst.State.PLAYING = _playing

_null = MagicMock(name='NULL')
_null.value_nick = 'null'
Gst.State.NULL = _null
Gst.StateChangeReturn.SUCCESS = 'GST_STATE_CHANGE_SUCCESS'
Gst.StateChangeReturn.FAILURE = 'GST_STATE_CHANGE_FAILURE'
Gst.SECOND = 1_000_000_000
Gst.Structure.from_string = MagicMock(return_value=(MagicMock(name='struct'), True))

GLib = MagicMock(name='GLib')
GLib.PRIORITY_DEFAULT = 0

GstVideo = MagicMock(name='GstVideo')
GstSdp = MagicMock(name='GstSdp')
GstSdp.SDPMessage.new_from_text = MagicMock(return_value=(MagicMock(name='ok'), MagicMock(name='sdp_obj')))
GstWebRTC = MagicMock(name='GstWebRTC')
GstWebRTC.WebRTCDataChannelState.OPEN = 'OPEN'

gi_repository = MagicMock(name='gi.repository')
gi_repository.Gst = Gst
gi_repository.GLib = GLib
gi_repository.GstVideo = GstVideo
gi_repository.GstSdp = GstSdp
gi_repository.GstWebRTC = GstWebRTC

gi_mock = MagicMock(name='gi')
gi_mock.repository = gi_repository

sys.modules['gi'] = gi_mock
sys.modules['gi.repository'] = gi_repository
sys.modules['gi.repository.Gst'] = Gst
sys.modules['gi.repository.GLib'] = GLib
sys.modules['gi.repository.GstVideo'] = GstVideo
sys.modules['gi.repository.GstSdp'] = GstSdp
sys.modules['gi.repository.GstWebRTC'] = GstWebRTC
