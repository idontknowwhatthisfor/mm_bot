#!/bin/bash
# Plays test games and keeps one log per game in logs/, then sums up what to look at.
#   ./run-tests.sh                 3 games of Tokyo at 4x
#   ./run-tests.sh LosAngeles 5 4  map, games, speed
#   ./run-tests.sh Tokyo 6 4 --no-jam-cost   anything after the speed goes to bot.py
set -u
cd "$(dirname "$0")"
map="${1:-Tokyo}"
games="${2:-3}"
speed="${3:-4}"
extra=("${@:4}")
mkdir -p logs
stamp="$(date +%Y%m%d-%H%M)"
logs=()
for n in $(seq 1 "$games"); do
    log="logs/test-$stamp-$n.log"
    logs+=("$log")
    echo "game $n of $games ($map at ${speed}x${extra[*]+ ${extra[*]}}), logging to $log"
    ./launch.sh
    sleep 6
    python3 bot.py --start "$map" --speed "$speed" "${extra[@]+"${extra[@]}"}" >"$log" 2>&1
done

echo
echo "summary"
for log in "${logs[@]}"; do
    count() { grep -c "$1" "$log"; }
    echo "$log"
    echo "  $(grep -h 'game over' "$log" || echo 'no game over line (crashed or stopped?)')"
    echo "  separate roads: $(count 'separate road for house'), cuts: $(count 'so no road carries'),"\
         "nothing left to draw: $(count 'nothing left to draw'), slow optimise passes: $(count 'optimising took')"
    echo "  dead-ended right after drawing: $(count 'dead-ended'), refused segments: $(count 'segments refused')"
    echo "  could not connect: $(count "no road found") with no road found, $(count "new road tiles, ") short of concrete"
    grep -h 'Traceback\|Error' "$log" | head -3 | sed 's/^/  /'
done

echo
echo "games, how they were lost, routes and queues (tools/summarize.py)"
python3 tools/summarize.py "${logs[@]}"
