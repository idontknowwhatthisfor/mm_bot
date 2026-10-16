"""Hold the running game at N times speed while you play it yourself (no bot).

    python3 speed.py [N]      # default 4; Ctrl-C puts the game back to normal speed
"""
import sys
import time

from game import Session
from mmbridge import Bridge, BridgeError


def main():
    speed = float(sys.argv[1]) if len(sys.argv) > 1 else 4.0
    with Bridge(timeout=120) as bridge:
        session = Session(bridge)
        print(f"waiting for a game; will hold it at {speed}x (Ctrl-C to stop)")
        try:
            while True:
                if session.attach():
                    try:
                        if abs(session.game_speed() - speed) > 1e-3:
                            session.set_game_speed(speed)
                            print(f"game set to {speed}x")
                    except BridgeError as e:
                        print(f"can't change the game speed: {e}")
                        return
                time.sleep(1.0)
        except KeyboardInterrupt:
            pass
        finally:
            try:
                session.set_game_speed(1.0)
            except BridgeError:
                pass
            print("game back at normal speed")


if __name__ == "__main__":
    main()
