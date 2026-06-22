package model

// PolicyStatus values match the CHECK constraint in 001_core.sql.
type PolicyStatus string

const (
	PolicyStatusAllowed            PolicyStatus = "allowed"
	PolicyStatusIndirectPublicOnly PolicyStatus = "indirect_public_only"
	PolicyStatusExcluded           PolicyStatus = "excluded"
)

// CoverageStatus values match the CHECK constraint in 001_core.sql.
type CoverageStatus string

const (
	CoverageStatusDirectPublicArchive  CoverageStatus = "direct_public_archive"
	CoverageStatusDelegatedToForeign   CoverageStatus = "delegated_to_foreign_authority"
	CoverageStatusRegionalRAIO         CoverageStatus = "regional_raio"
	CoverageStatusOfficialContactOnly  CoverageStatus = "official_contact_only"
	CoverageStatusSourceExistsUnstable CoverageStatus = "source_exists_unstable"
	CoverageStatusNoPublicArchive      CoverageStatus = "no_public_archive"
	CoverageStatusPolicyExcluded       CoverageStatus = "policy_excluded"
	CoverageStatusUnknown              CoverageStatus = "unknown"
)

// AuthorityType values match the CHECK constraint in 001_core.sql (authorities.type).
type AuthorityType string

const (
	AuthorityTypeNationalAAI          AuthorityType = "national_aai"
	AuthorityTypeCAA                  AuthorityType = "caa"
	AuthorityTypeMinistry             AuthorityType = "ministry"
	AuthorityTypeRegionalRAIO         AuthorityType = "regional_raio"
	AuthorityTypeForeignAAI           AuthorityType = "foreign_aai"
	AuthorityTypeManufacturerStateAAI AuthorityType = "manufacturer_state_aai"
	AuthorityTypeOperatorStateAAI     AuthorityType = "operator_state_aai"
	AuthorityTypeRegistryStateAAI     AuthorityType = "registry_state_aai"
)

// AuthorityStatus / SourceHealthStatus values match the shared CHECK constraint
// used in authorities.status and sources.health_status.
type HealthStatus string

const (
	HealthStatusOK                 HealthStatus = "ok"
	HealthStatusEmptyArchive       HealthStatus = "empty_archive"
	HealthStatusTLSError           HealthStatus = "tls_error"
	HealthStatusNXDomain           HealthStatus = "nx_domain"
	HealthStatusSuspended          HealthStatus = "suspended"
	HealthStatusForbidden          HealthStatus = "forbidden"
	HealthStatusChangedStructure   HealthStatus = "changed_structure"
	HealthStatusManualReviewNeeded HealthStatus = "manual_review_needed"
	HealthStatusUnknown            HealthStatus = "unknown"
)

// BodyClass values match the CHECK constraint in 001_core.sql (regional_bodies.body_class).
type BodyClass string

const (
	BodyClassRAIO         BodyClass = "raio"
	BodyClassICM          BodyClass = "icm"
	BodyClassRegionalBody BodyClass = "regional_body"
)

// SourceType values match the CHECK constraint in 001_core.sql (sources.source_type).
type SourceType string

const (
	SourceOfficialAAI                  SourceType = "official_aai"
	SourceOfficialForeignAccreditedRep SourceType = "official_foreign_accredited_rep"
	SourceICAOELibrary                 SourceType = "icao_elibrary"
	SourceRegulator                    SourceType = "regulator"
	SourceMinistry                     SourceType = "ministry"
	SourceOperator                     SourceType = "operator"
	SourceManufacturer                 SourceType = "manufacturer"
	SourceRegionalBody                 SourceType = "regional_body"
	SourceTrustedIndex                 SourceType = "trusted_index"
	SourceMedia                        SourceType = "media"
	SourceWayback                      SourceType = "wayback"
)

// DatePrecision values match the CHECK constraint in 002_pipeline.sql (events.date_precision).
type DatePrecision string

const (
	DatePrecisionExact   DatePrecision = "exact"
	DatePrecisionMonth   DatePrecision = "month"
	DatePrecisionYear    DatePrecision = "year"
	DatePrecisionUnknown DatePrecision = "unknown"
)

// EventType values match the CHECK constraint in 002_pipeline.sql (events.event_type).
type EventType string

const (
	EventTypeAccident        EventType = "accident"
	EventTypeSeriousIncident EventType = "serious_incident"
	EventTypeIncident        EventType = "incident"
	EventTypeHijacking       EventType = "hijacking"
	EventTypeUnknown         EventType = "unknown"
)

// InvestigationStatus values match the CHECK constraint in 002_pipeline.sql.
type InvestigationStatus string

const (
	InvestigationStatusFinalReportAvailable       InvestigationStatus = "final_report_available"
	InvestigationStatusPreliminaryReportAvailable InvestigationStatus = "preliminary_report_available"
	InvestigationStatusOpen                       InvestigationStatus = "investigation_open"
	InvestigationStatusNoReportFound              InvestigationStatus = "no_report_found"
	InvestigationStatusUnknown                    InvestigationStatus = "unknown"
)

// DedupStatus values match the CHECK constraint in 002_pipeline.sql (events.dedup_status).
type DedupStatus string

const (
	DedupStatusUnreviewed   DedupStatus = "unreviewed"
	DedupStatusAutoMerged   DedupStatus = "auto_merged"
	DedupStatusSoftLinked   DedupStatus = "soft_linked"
	DedupStatusManualReview DedupStatus = "manual_review"
	DedupStatusDistinct     DedupStatus = "distinct"
)

// ReportType values match the CHECK constraint in 002_pipeline.sql (reports.report_type).
type ReportType string

const (
	ReportTypeFinal                ReportType = "final"
	ReportTypePreliminary          ReportType = "preliminary"
	ReportTypeInterim              ReportType = "interim"
	ReportTypeFactual              ReportType = "factual"
	ReportTypeSafetyRecommendation ReportType = "safety_recommendation"
	ReportTypePressRelease         ReportType = "press_release"
	ReportTypeIndexRecord          ReportType = "index_record"
	ReportTypeMediaArticle         ReportType = "media_article"
)

// ExtractionStatus values match the CHECK constraint in 002_pipeline.sql.
type ExtractionStatus string

const (
	ExtractionStatusPending      ExtractionStatus = "pending"
	ExtractionStatusExtracted    ExtractionStatus = "extracted"
	ExtractionStatusFailed       ExtractionStatus = "failed"
	ExtractionStatusManualReview ExtractionStatus = "manual_review"
)

// CopyrightStatus values match the CHECK constraint in 002_pipeline.sql.
type CopyrightStatus string

const (
	CopyrightStatusOfficialPublic     CopyrightStatus = "official_public"
	CopyrightStatusMetadataOnly       CopyrightStatus = "metadata_only"
	CopyrightStatusUnknown            CopyrightStatus = "unknown"
	CopyrightStatusDoNotStoreFulltext CopyrightStatus = "do_not_store_fulltext"
)

// InvestigationRole values match the CHECK constraint in 002_pipeline.sql.
type InvestigationRole string

const (
	InvestigationRoleStateOfOccurrence        InvestigationRole = "state_of_occurrence"
	InvestigationRoleStateOfRegistry          InvestigationRole = "state_of_registry"
	InvestigationRoleStateOfOperator          InvestigationRole = "state_of_operator"
	InvestigationRoleStateOfDesign            InvestigationRole = "state_of_design"
	InvestigationRoleStateOfManufacture       InvestigationRole = "state_of_manufacture"
	InvestigationRoleStateOfEngineManufacture InvestigationRole = "state_of_engine_manufacture"
	InvestigationRoleAccreditedRepresentative InvestigationRole = "accredited_representative"
	InvestigationRoleDelegatedInvestigator    InvestigationRole = "delegated_investigator"
	InvestigationRoleAssistingAuthority       InvestigationRole = "assisting_authority"
)

// CrawlJobType values match the CHECK constraint in 002_pipeline.sql.
type CrawlJobType string

const (
	CrawlJobTypeAuthorityHealthCheck CrawlJobType = "authority_health_check"
	CrawlJobTypeArchiveCrawl         CrawlJobType = "archive_crawl"
	CrawlJobTypePDFDiscovery         CrawlJobType = "pdf_discovery"
	CrawlJobTypeICAOELibrarySearch   CrawlJobType = "icao_elibrary_search"
	CrawlJobTypeNTSBForeignSearch    CrawlJobType = "ntsb_foreign_search"
	CrawlJobTypeBEAForeignSearch     CrawlJobType = "bea_foreign_search"
	CrawlJobTypeATSBSearch           CrawlJobType = "atsb_search"
	CrawlJobTypeWaybackCDX           CrawlJobType = "wayback_cdx"
	CrawlJobTypeDirectRequestNeeded  CrawlJobType = "direct_request_needed"
)

// CrawlJobStatus values match the CHECK constraint in 002_pipeline.sql.
type CrawlJobStatus string

const (
	CrawlJobStatusPending      CrawlJobStatus = "pending"
	CrawlJobStatusRunning      CrawlJobStatus = "running"
	CrawlJobStatusSuccess      CrawlJobStatus = "success"
	CrawlJobStatusFailed       CrawlJobStatus = "failed"
	CrawlJobStatusPartial      CrawlJobStatus = "partial"
	CrawlJobStatusManualReview CrawlJobStatus = "manual_review"
)

// CrawlErrorType values match the CHECK constraint in 002_pipeline.sql.
type CrawlErrorType string

const (
	CrawlErrorTypeTLSError      CrawlErrorType = "tls_error"
	CrawlErrorTypeTimeout       CrawlErrorType = "timeout"
	CrawlErrorTypeDNSError      CrawlErrorType = "dns_error"
	CrawlErrorTypeNXDomain      CrawlErrorType = "nx_domain"
	CrawlErrorTypeHTTP403       CrawlErrorType = "http_403"
	CrawlErrorTypeHTTP404       CrawlErrorType = "http_404"
	CrawlErrorTypeHTTP500       CrawlErrorType = "http_500"
	CrawlErrorTypeParseError    CrawlErrorType = "parse_error"
	CrawlErrorTypeRobotsBlocked CrawlErrorType = "robots_blocked"
	CrawlErrorTypeUnknown       CrawlErrorType = "unknown"
)

// ImportRunStatus values match the CHECK constraint in 003_provenance.sql.
type ImportRunStatus string

const (
	ImportRunStatusRunning   ImportRunStatus = "running"
	ImportRunStatusSuccess   ImportRunStatus = "success"
	ImportRunStatusPartial   ImportRunStatus = "partial"
	ImportRunStatusFailed    ImportRunStatus = "failed"
	ImportRunStatusUnchanged ImportRunStatus = "unchanged"
)

// ProvenanceKind values match the CHECK constraint in 003_provenance.sql.
type ProvenanceKind string

const (
	ProvenanceKindSeed            ProvenanceKind = "seed"
	ProvenanceKindICAOSnapshot    ProvenanceKind = "icao_snapshot"
	ProvenanceKindCuratedOverride ProvenanceKind = "curated_override"
)

// ConflictReviewStatus values match the CHECK constraint in 003_provenance.sql.
type ConflictReviewStatus string

const (
	ConflictReviewStatusOpen             ConflictReviewStatus = "open"
	ConflictReviewStatusAcceptedIncoming ConflictReviewStatus = "accepted_incoming"
	ConflictReviewStatusKeptCurated      ConflictReviewStatus = "kept_curated"
	ConflictReviewStatusResolvedManually ConflictReviewStatus = "resolved_manually"
)

// AuthorityRequestStatus values match the CHECK constraint in 003_provenance.sql.
type AuthorityRequestStatus string

const (
	AuthorityRequestStatusNotSent    AuthorityRequestStatus = "not_sent"
	AuthorityRequestStatusSent       AuthorityRequestStatus = "sent"
	AuthorityRequestStatusReplied    AuthorityRequestStatus = "replied"
	AuthorityRequestStatusBounced    AuthorityRequestStatus = "bounced"
	AuthorityRequestStatusNoResponse AuthorityRequestStatus = "no_response"
)

// CountryGroup values match the CHECK constraint in 001_core.sql (country_group).
type CountryGroup string

const (
	CountryGroupA  CountryGroup = "A"
	CountryGroupB  CountryGroup = "B"
	CountryGroupC1 CountryGroup = "C1"
	CountryGroupC2 CountryGroup = "C2"
	CountryGroupC3 CountryGroup = "C3"
	CountryGroupD  CountryGroup = "D"
)
