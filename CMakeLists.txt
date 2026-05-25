cmake_minimum_required(VERSION 3.20)

project(tidal_bitperfect_native LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_AUTOMOC ON)

find_package(PkgConfig REQUIRED)
find_package(Qt6 REQUIRED COMPONENTS Core Widgets Network DBus)
find_package(Python3 QUIET COMPONENTS Interpreter)
pkg_check_modules(ALSA REQUIRED IMPORTED_TARGET alsa)
pkg_check_modules(SNDFILE REQUIRED IMPORTED_TARGET sndfile)

add_executable(tidal-native-player
    native/tidal_native_player.cpp
    native/daemon_protocol.cpp
    native/alsa_output.cpp
    native/ffmpeg_child.cpp
    native/playback_state.cpp
)
target_link_libraries(tidal-native-player PRIVATE PkgConfig::ALSA PkgConfig::SNDFILE)
target_compile_options(tidal-native-player PRIVATE -Wall -Wextra -Wpedantic)

add_executable(tidal-qt6
    qt6/main.cpp
    qt6/browser_controller.cpp
    qt6/lyrics_controller.cpp
    qt6/main_window_integrations.cpp
    qt6/main_window.cpp
    qt6/main_window_support.cpp
    qt6/settings_dialog.cpp
    qt6/playback_controller.cpp
    qt6/scrobble_service.cpp
    qt6/tidal_client.cpp
    qt6/tidal_client_auth.cpp
    qt6/tidal_client_http.cpp
    qt6/tidal_client_endpoints.cpp
    qt6/tidal_client_models.cpp
    qt6/native_playback_client.cpp
    qt6/cache_manager.cpp
    qt6/tidal_api_client.cpp
    qt6/runtime_paths.cpp
    qt6/mpris_service.cpp
    qt6/discord_rpc_service.cpp
)
target_include_directories(tidal-qt6 PRIVATE qt6)
target_compile_definitions(tidal-qt6 PRIVATE TIDAL_SOURCE_DIR="${CMAKE_SOURCE_DIR}")
target_link_libraries(tidal-qt6 PRIVATE Qt6::Core Qt6::Widgets Qt6::Network Qt6::DBus)
target_compile_options(tidal-qt6 PRIVATE -Wall -Wextra -Wpedantic)

set(TIDAL_QT6_ICON_SOURCE "${CMAKE_SOURCE_DIR}/packaging/linux/tidal-bitperfect-qt6.svg")
file(GENERATE
    OUTPUT "${CMAKE_BINARY_DIR}/tidal-bitperfect-qt6.desktop"
    CONTENT "[Desktop Entry]\nType=Application\nName=TIDAL Bitperfect Qt6\nComment=Native Qt6 TIDAL player with direct ALSA output\nExec=$<TARGET_FILE:tidal-qt6>\nIcon=${TIDAL_QT6_ICON_SOURCE}\nTerminal=false\nCategories=AudioVideo;Audio;Player;\nStartupNotify=true\nStartupWMClass=tidal-bitperfect-qt6\n"
    FILE_PERMISSIONS OWNER_READ OWNER_WRITE OWNER_EXECUTE GROUP_READ GROUP_EXECUTE WORLD_READ WORLD_EXECUTE
)

install(TARGETS tidal-native-player RUNTIME DESTINATION bin)
install(TARGETS tidal-qt6 RUNTIME DESTINATION bin)
install(FILES packaging/linux/tidal-bitperfect-qt6.desktop DESTINATION share/applications)
install(FILES "${TIDAL_QT6_ICON_SOURCE}" DESTINATION share/icons/hicolor/scalable/apps)
install(FILES packaging/linux/tidal-bitperfect-transparent.svg DESTINATION share/tidal-bitperfect)

if(Python3_Interpreter_FOUND)
    add_custom_target(smoke
        COMMAND "${Python3_EXECUTABLE}" "${CMAKE_SOURCE_DIR}/qt6/media_contract_smoke.py"
        COMMAND "${Python3_EXECUTABLE}" "${CMAKE_SOURCE_DIR}/qt6/native_gapless_smoke.py" "$<TARGET_FILE:tidal-native-player>"
        DEPENDS tidal-native-player tidal-qt6
        WORKING_DIRECTORY "${CMAKE_SOURCE_DIR}"
        COMMENT "Running Qt6 media and native gapless smoke checks"
    )
endif()
