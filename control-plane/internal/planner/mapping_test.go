package planner

import (
	"reflect"
	"testing"

	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/model"
)

func TestJobTypesFor(t *testing.T) {
	cases := []struct {
		name     string
		status   model.CoverageStatus
		delegate string
		want     []model.CrawlJobType
	}{
		{"direct_archive", model.CoverageStatusDirectPublicArchive, "",
			[]model.CrawlJobType{model.CrawlJobTypeAuthorityHealthCheck, model.CrawlJobTypeArchiveCrawl}},
		{"unstable", model.CoverageStatusSourceExistsUnstable, "",
			[]model.CrawlJobType{model.CrawlJobTypeArchiveCrawl, model.CrawlJobTypeWaybackCDX}},
		{"regional", model.CoverageStatusRegionalRAIO, "",
			[]model.CrawlJobType{model.CrawlJobTypeArchiveCrawl, model.CrawlJobTypeWaybackCDX}},
		{"contact_only", model.CoverageStatusOfficialContactOnly, "",
			[]model.CrawlJobType{model.CrawlJobTypeDirectRequestNeeded, model.CrawlJobTypeICAOELibrarySearch}},
		{"no_archive", model.CoverageStatusNoPublicArchive, "",
			[]model.CrawlJobType{model.CrawlJobTypeWaybackCDX, model.CrawlJobTypePDFDiscovery, model.CrawlJobTypeDirectRequestNeeded}},
		{"unknown", model.CoverageStatusUnknown, "",
			[]model.CrawlJobType{model.CrawlJobTypeICAOELibrarySearch, model.CrawlJobTypeWaybackCDX}},
		{"delegated_FR", model.CoverageStatusDelegatedToForeign, "FR",
			[]model.CrawlJobType{model.CrawlJobTypeBEAForeignSearch, model.CrawlJobTypeICAOELibrarySearch}},
		{"delegated_US", model.CoverageStatusDelegatedToForeign, "US",
			[]model.CrawlJobType{model.CrawlJobTypeNTSBForeignSearch, model.CrawlJobTypeICAOELibrarySearch}},
		{"delegated_unknown", model.CoverageStatusDelegatedToForeign, "ES",
			[]model.CrawlJobType{model.CrawlJobTypeICAOELibrarySearch, model.CrawlJobTypeWaybackCDX}},
		{"delegated_AU", model.CoverageStatusDelegatedToForeign, "AU",
			[]model.CrawlJobType{model.CrawlJobTypeATSBSearch, model.CrawlJobTypeICAOELibrarySearch}},
		{"policy_excluded", model.CoverageStatusPolicyExcluded, "", nil},
		{"delegated_empty", model.CoverageStatusDelegatedToForeign, "",
			[]model.CrawlJobType{model.CrawlJobTypeICAOELibrarySearch, model.CrawlJobTypeWaybackCDX}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := JobTypesFor(tc.status, tc.delegate)
			if !reflect.DeepEqual(got, tc.want) {
				t.Fatalf("JobTypesFor(%q,%q) = %v, want %v", tc.status, tc.delegate, got, tc.want)
			}
		})
	}
}
