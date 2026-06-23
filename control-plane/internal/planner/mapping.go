// Package planner turns the control-plane's coverage map into ranked crawl jobs.
package planner

import "github.com/denyskolomiiets/aviation-safety-scrapers/control-plane/internal/model"

// foreignSearchByDelegate maps an accredited-representative state to its
// foreign-search job type. Only the three states with a dedicated job type are
// present; any other delegate uses the safe fallback in JobTypesFor.
var foreignSearchByDelegate = map[string]model.CrawlJobType{
	"US": model.CrawlJobTypeNTSBForeignSearch,
	"FR": model.CrawlJobTypeBEAForeignSearch,
	"AU": model.CrawlJobTypeATSBSearch,
}

func foreignSearchFor(delegateISO2 string) (model.CrawlJobType, bool) {
	jt, ok := foreignSearchByDelegate[delegateISO2]
	return jt, ok
}

// staticMapping is the coverage_status → job types table for every status that
// does not depend on the delegate.
var staticMapping = map[model.CoverageStatus][]model.CrawlJobType{
	model.CoverageStatusDirectPublicArchive:  {model.CrawlJobTypeAuthorityHealthCheck, model.CrawlJobTypeArchiveCrawl},
	model.CoverageStatusSourceExistsUnstable: {model.CrawlJobTypeArchiveCrawl, model.CrawlJobTypeWaybackCDX},
	model.CoverageStatusRegionalRAIO:         {model.CrawlJobTypeArchiveCrawl, model.CrawlJobTypeWaybackCDX},
	model.CoverageStatusOfficialContactOnly:  {model.CrawlJobTypeDirectRequestNeeded, model.CrawlJobTypeICAOELibrarySearch},
	model.CoverageStatusNoPublicArchive:      {model.CrawlJobTypeWaybackCDX, model.CrawlJobTypePDFDiscovery, model.CrawlJobTypeDirectRequestNeeded},
	model.CoverageStatusUnknown:              {model.CrawlJobTypeICAOELibrarySearch, model.CrawlJobTypeWaybackCDX},
}

// delegateFallback is used when a delegated country has no recognised delegate.
var delegateFallback = []model.CrawlJobType{model.CrawlJobTypeICAOELibrarySearch, model.CrawlJobTypeWaybackCDX}

// JobTypesFor returns the job types to schedule for a country's coverage status.
// policy_excluded returns nil (such countries are filtered out before mapping).
func JobTypesFor(status model.CoverageStatus, delegateISO2 string) []model.CrawlJobType {
	if status == model.CoverageStatusDelegatedToForeign {
		if jt, ok := foreignSearchFor(delegateISO2); ok {
			return []model.CrawlJobType{jt, model.CrawlJobTypeICAOELibrarySearch}
		}
		return append([]model.CrawlJobType(nil), delegateFallback...)
	}
	if jts, ok := staticMapping[status]; ok {
		return append([]model.CrawlJobType(nil), jts...)
	}
	return nil
}
