"""Tests for the [from ccmux] relay-tag helpers."""

from ccmux_telegram.relay_tag import RELAY_TAG_PREFIX, strip_relay_tag, tag_relayed


class TestTagRelayed:
    def test_prepends_tag_with_space(self):
        assert tag_relayed("hello") == "[from ccmux] hello"

    def test_works_on_empty_string(self):
        assert tag_relayed("") == RELAY_TAG_PREFIX

    def test_does_not_collapse_internal_whitespace(self):
        assert tag_relayed("a  b\nc") == "[from ccmux] a  b\nc"


class TestStripRelayTag:
    def test_strips_when_prefix_present(self):
        assert strip_relay_tag("[from ccmux] hello") == "hello"

    def test_passes_through_when_prefix_absent(self):
        assert strip_relay_tag("hello") == "hello"

    def test_does_not_strip_partial_match(self):
        # Without the trailing space the message is something the user
        # typed themselves, not a relayed one. Leave it alone.
        assert strip_relay_tag("[from ccmux]hello") == "[from ccmux]hello"

    def test_round_trip_is_identity(self):
        assert strip_relay_tag(tag_relayed("anything")) == "anything"
