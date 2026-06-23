package model

import "testing"

func TestSourceTierAllowsWaybackAtTier5(t *testing.T) {
	if !SourceTierAllowsType(5, SourceWayback) {
		t.Fatal("tier 5 should allow wayback source type")
	}
	if SourceTierAllowsType(1, SourceWayback) {
		t.Fatal("tier 1 should not allow wayback source type")
	}
}
