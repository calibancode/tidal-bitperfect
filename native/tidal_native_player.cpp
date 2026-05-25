#include "native_player_internal.h"

int main(int argc, char** argv) {
    try {
        return tidal_native::run(argc, argv);
    } catch (const std::exception& exc) {
        tidal_native::emit_line("ERROR", exc.what());
        std::cerr << exc.what() << '\n';
        return 1;
    }
}
