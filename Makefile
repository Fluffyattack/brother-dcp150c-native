# SPDX-License-Identifier: GPL-2.0-or-later
CC ?= cc
AR ?= ar
BUILD ?= build
CPPFLAGS ?=
CFLAGS ?= -O2 -Wall -Wextra -Wconversion -Werror -std=c11
ARCHFLAGS ?=
LDFLAGS ?=
RUNTIME_DEP ?=
COMPONENT_SOURCES = src/calibration.c src/codecs.c src/color.c src/device.c \
	src/input.c src/planes.c src/profile.c src/rows.c src/pipeline.c
COMPONENT_OBJECTS = $(patsubst src/%.c,$(BUILD)/%.o,$(COMPONENT_SOURCES))
COMPONENT_HEADERS = $(wildcard src/*.h)

.PHONY: all config-tools components armv6 clean

all: config-tools components $(BUILD)/bh7filter

components: $(BUILD)/libbh7components.a

$(BUILD)/libbh7components.a: $(COMPONENT_OBJECTS)
	$(AR) rcs $@ $^

$(BUILD)/%.o: src/%.c $(COMPONENT_HEADERS) $(RUNTIME_DEP) | $(BUILD)
	$(CC) $(CPPFLAGS) $(ARCHFLAGS) $(CFLAGS) -c $< -o $@

$(BUILD)/bh7filter: src/filter.c $(BUILD)/libbh7components.a $(COMPONENT_HEADERS) $(RUNTIME_DEP) | $(BUILD)
	$(CC) $(CPPFLAGS) $(ARCHFLAGS) $(CFLAGS) $< $(BUILD)/libbh7components.a $(LDFLAGS) -o $@

config-tools: $(BUILD)/brprintconf_dcp150c $(BUILD)/brprintconfij2

$(BUILD):
	mkdir -p $@

$(BUILD)/brprintconf_dcp150c: src/printconfig.c $(RUNTIME_DEP) | $(BUILD)
	$(CC) $(CPPFLAGS) $(ARCHFLAGS) $(CFLAGS) $< $(LDFLAGS) -o $@

$(BUILD)/brprintconfij2: $(BUILD)/brprintconf_dcp150c
	ln -sfn brprintconf_dcp150c $@

armv6:
	bash scripts/build-armv6-toolchain.sh
	$(MAKE) CC='$(CURDIR)/build/toolchain-armv6/bin/musl-gcc' \
		AR=arm-linux-gnueabihf-ar BUILD=build/armv6 \
		RUNTIME_DEP='$(CURDIR)/build/toolchain-armv6/lib/libc.a $(CURDIR)/build/toolchain-armv6/bin/musl-gcc' \
		ARCHFLAGS='-marm -march=armv6 -mfpu=vfp -mfloat-abi=hard' \
		LDFLAGS='-static' all

clean:
	rm -rf -- $(BUILD)
