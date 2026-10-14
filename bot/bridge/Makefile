# Builds the bridge dylib that launch.sh injects into Mini Motorways.
OUT := build/libmmbridge.dylib

$(OUT): mmbridge.m
	@mkdir -p build
	clang -arch arm64 -dynamiclib -fobjc-arc -fobjc-arc-exceptions -O2 -Wall -Wextra \
		-Wno-unused-parameter -framework Foundation -o $@ $<
	codesign -s - -f $@

clean:
	rm -rf build

.PHONY: clean
