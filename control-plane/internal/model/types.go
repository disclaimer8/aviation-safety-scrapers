package model

import (
	"strings"
	"unicode"

	"golang.org/x/text/unicode/norm"
)

// NormalizeName folds a free-text authority/source name to a canonical
// lowercase ASCII form suitable for deduplication:
//   - trim leading/trailing whitespace
//   - lower-case
//   - normalize typographic apostrophes and dashes to ASCII equivalents
//   - strip diacritics (NFD decompose then drop Mn combining marks)
//   - collapse internal whitespace runs to a single space
func NormalizeName(s string) string {
	s = strings.TrimSpace(strings.ToLower(s))
	replacer := strings.NewReplacer(
		"’", "'", // right single quotation mark → apostrophe
		"‘", "'", // left single quotation mark → apostrophe
		"–", "-", // en-dash → hyphen
		"—", "-", // em-dash → hyphen
	)
	s = replacer.Replace(s)
	s = norm.NFD.String(s)
	s = strings.Map(func(r rune) rune {
		if unicode.Is(unicode.Mn, r) {
			return -1
		}
		return r
	}, s)
	return strings.Join(strings.Fields(s), " ")
}

// PriorityScore computes a simple priority ranking for a coverage candidate.
// Formula: (expectedRecords * quality) / effort.
// Returns 0 when effort is zero to avoid division-by-zero.
func PriorityScore(expectedRecords, quality, effort int) float64 {
	if effort <= 0 {
		return 0
	}
	return float64(expectedRecords*quality) / float64(effort)
}

// SourceTierAllowsType reports whether a source of the given tier may have the
// given source_type. The mapping mirrors the schema semantics:
//
//	Tier 1 → official_aai
//	Tier 2 → official_foreign_accredited_rep
//	Tier 3 → icao_elibrary
//	Tier 4 → regulator, ministry, operator, manufacturer, regional_body
//	Tier 5 → trusted_index
//	Tier 6 → media
//
// wayback is not in any tier's canonical allowlist (Tier 6 only covers media).
func SourceTierAllowsType(tier int, typ SourceType) bool {
	switch tier {
	case 1:
		return typ == SourceOfficialAAI
	case 2:
		return typ == SourceOfficialForeignAccreditedRep
	case 3:
		return typ == SourceICAOELibrary
	case 4:
		return typ == SourceRegulator || typ == SourceMinistry ||
			typ == SourceOperator || typ == SourceManufacturer || typ == SourceRegionalBody
	case 5:
		return typ == SourceTrustedIndex || typ == SourceWayback
	case 6:
		return typ == SourceMedia
	default:
		return false
	}
}
