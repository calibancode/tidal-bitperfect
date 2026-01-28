"""
Discord Rich Presence integration for TIDAL Bitperfect.

This module handles Discord RPC updates to show currently playing tracks
with album art, track info, and playback progress.
"""

import time
import logging
from typing import Optional, Dict, Any
from PySide6 import QtCore

try:
    from pypresence import Presence, DiscordNotFound, InvalidID, InvalidPipe, ActivityType
    PYPRESENCE_AVAILABLE = True
except Exception:  # pragma: no cover - best effort import guard
    Presence = None
    DiscordNotFound = None
    InvalidID = None
    InvalidPipe = None
    ActivityType = None
    PYPRESENCE_AVAILABLE = False

logger = logging.getLogger(__name__)

# Default Discord application ID for TIDAL Bitperfect
# This is pre-configured so users don't need to create their own Discord app
DEFAULT_CLIENT_ID = "1465929585698017426"


class DiscordRPC(QtCore.QObject):
    """
    Manages Discord Rich Presence for TIDAL playback.

    Connects to Discord via IPC and updates the user's status with:
    - Track title and artist
    - Album name
    - Album artwork (via external URL)
    - Playback progress and duration
    - Audio quality information
    """

    # Signals for status updates
    status_message = QtCore.Signal(str)  # Emits status messages (connected, disconnected, etc.)
    error_message = QtCore.Signal(str)   # Emits error messages

    def __init__(self, client_id: str, parent: Optional[QtCore.QObject] = None):
        """
        Initialize Discord RPC manager.

        Args:
            client_id: Discord application client ID
            parent: Optional parent QObject
        """
        super().__init__(parent)
        if not PYPRESENCE_AVAILABLE:
            raise RuntimeError("pypresence is not installed")
        self.client_id = client_id
        self.rpc: Optional[Presence] = None
        self.connected = False

        # Current state
        self._current_track: Optional[Dict[str, Any]] = None
        self._is_playing = False
        self._quality_info: Optional[str] = None

    def connect(self) -> bool:
        """
        Connect to Discord client.

        Returns:
            True if connected successfully, False otherwise
        """
        if self.connected:
            return True

        try:
            self.rpc = Presence(self.client_id)
            self.rpc.connect()
            self.connected = True
            logger.info("Connected to Discord Rich Presence")
            self.status_message.emit("Discord Rich Presence connected")
            return True
        except (DiscordNotFound, InvalidID, InvalidPipe) as e:
            logger.warning(f"Failed to connect to Discord: {e}")
            self.error_message.emit(f"Discord connection failed: {e}")
            self.rpc = None
            self.connected = False
            return False
        except Exception as e:
            logger.error(f"Unexpected error connecting to Discord: {e}")
            self.error_message.emit(f"Discord error: {e}")
            self.rpc = None
            self.connected = False
            return False

    def disconnect(self):
        """Disconnect from Discord and clear presence."""
        if self.rpc and self.connected:
            try:
                self.rpc.clear()
                self.rpc.close()
                logger.info("Disconnected from Discord Rich Presence")
            except Exception as e:
                logger.warning(f"Error disconnecting from Discord: {e}")
            finally:
                self.rpc = None
                self.connected = False
                self._current_track = None
                self._is_playing = False
                self.status_message.emit("Discord Rich Presence disconnected")

    def update_track(self, track: Dict[str, Any], quality_info: Optional[Dict[str, Any]] = None):
        """
        Update Discord presence with new track information.

        Args:
            track: Track dictionary with keys: id, title, artist, album, duration
            quality_info: Optional dict with audio_quality, bit_depth, sample_rate
        """
        if not self.connected:
            if not self.connect():
                return

        self._current_track = track
        self._is_playing = True

        # Format quality info
        if quality_info:
            quality = quality_info.get('audio_quality', '')
            bit_depth = quality_info.get('bit_depth')
            sample_rate = quality_info.get('sample_rate')

            # Build quality string with label and/or technical specs
            parts = []

            if quality:
                # Map quality values to display names
                quality_map = {
                    'HI_RES_LOSSLESS': 'Max',
                    'LOSSLESS': 'HiFi',
                    'HIGH': 'High',
                    'LOW': 'Low',
                }
                quality_display = quality_map.get(quality, quality.replace('_', ' ').title())
                parts.append(quality_display)
            elif bit_depth and sample_rate:
                # Fallback: provide friendly labels for common high-quality formats without TIDAL labels
                if bit_depth >= 24 or sample_rate >= 48000:
                    parts.append('HiFi+')
                elif bit_depth == 16 and sample_rate == 44100:
                    parts.append('HiFi')

            if bit_depth and sample_rate:
                sample_rate_khz = sample_rate / 1000
                parts.append(f"{bit_depth}bit/{sample_rate_khz:.1f}kHz")

            self._quality_info = " • ".join(parts) if parts else None
        else:
            self._quality_info = None

        self._update_presence()

    def update_position(self, position_s: float, duration_s: float):
        """
        Update playback position (currently unused, kept for API compatibility).

        Args:
            position_s: Current position in seconds
            duration_s: Total duration in seconds
        """
        pass

    def set_playing(self, is_playing: bool):
        """
        Update play/pause state.

        Args:
            is_playing: True if playing, False if paused
        """
        if not self._current_track:
            return

        if is_playing and not self._is_playing:
            # Resuming from pause
            self._is_playing = True
            self._update_presence()
        elif not is_playing and self._is_playing:
            # Pausing
            self._is_playing = False
            self._update_presence()

    def stop(self):
        """Stop playback and clear presence."""
        if not self.connected:
            return

        try:
            if self.rpc:
                self.rpc.clear()
                logger.debug("Cleared Discord presence")
        except Exception as e:
            logger.warning(f"Error clearing Discord presence: {e}")
        finally:
            self._current_track = None
            self._is_playing = False
            self._quality_info = None

    def _update_presence(self):
        """Internal method to update Discord presence with current state."""
        if not self.rpc or not self.connected or not self._current_track:
            return

        track = self._current_track

        # Prepare presence data
        artist = track.get('artist', 'Unknown Artist')
        album = track.get('album', 'Unknown Album')
        title = track.get('title', 'Unknown Track')

        # Header: "Listening to [Artist]"
        # Line 1 (bold): Track name
        details = title

        # Line 2: Artist • Album (with paused indicator if needed)
        state = f"{artist} • {album}"
        if not self._is_playing:
            state = f"{state} (Paused)"

        # Line 3: Quality info (also serves as album art tooltip)
        large_text = self._quality_info if self._quality_info else album

        presence_data = {
            "activity_type": ActivityType.LISTENING,
            "name": artist,  # Shows as "Listening to [Artist]"
            "details": details,
            "state": state,
            "large_image": self._get_album_art_url(track),
            "large_text": large_text,  # Third line + album art tooltip
        }

        try:
            self.rpc.update(**presence_data)
            logger.debug(f"Updated Discord presence: {track.get('title')} by {track.get('artist')}")
        except Exception as e:
            logger.error(f"Error updating Discord presence: {e}")
            self.error_message.emit(f"Failed to update Discord: {e}")
            # Try to reconnect on next update
            self.connected = False

    def _get_album_art_url(self, track: Dict[str, Any]) -> str:
        """
        Get album art URL from track data.

        Discord supports external image URLs, so we can use TIDAL's CDN directly.

        Args:
            track: Track dictionary

        Returns:
            Album art URL or default logo identifier
        """
        # Check if track has cover_url (most common case)
        cover_url = track.get('cover_url')
        if cover_url:
            return cover_url

        # Fallback: check for album_obj (TIDAL API object)
        album = track.get('album_obj')
        if album and hasattr(album, 'image'):
            try:
                cover_uuid = album.image(1280)
                if cover_uuid:
                    return f"https://resources.tidal.com/images/{cover_uuid.replace('-', '/')}/1280x1280.jpg"
            except Exception:
                pass

        # Final fallback: use default TIDAL logo (user should upload this to Discord app)
        return "tidal_logo"
