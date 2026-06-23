package planner

import (
	"testing"

	"github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/model"
)

func TestSourceResolverResolvesEveryJobType(t *testing.T) {
	ctx, db := seededDB(t)
	r, err := NewSourceResolver(ctx, db)
	if err != nil {
		t.Fatal(err)
	}
	jobTypes := []model.CrawlJobType{
		model.CrawlJobTypeAuthorityHealthCheck,
		model.CrawlJobTypeArchiveCrawl,
		model.CrawlJobTypeWaybackCDX,
		model.CrawlJobTypePDFDiscovery,
		model.CrawlJobTypeICAOELibrarySearch,
		model.CrawlJobTypeDirectRequestNeeded,
		model.CrawlJobTypeNTSBForeignSearch,
		model.CrawlJobTypeBEAForeignSearch,
		model.CrawlJobTypeATSBSearch,
	}
	for _, jt := range jobTypes {
		id, ok := r.Resolve(jt)
		if !ok || id <= 0 {
			t.Errorf("Resolve(%q) = (%d,%v), want a real source id", jt, id, ok)
		}
	}
}
