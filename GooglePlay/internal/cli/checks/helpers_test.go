package checks

import (
	"errors"
	"testing"

	checksapi "google.golang.org/api/checks/v1alpha"
)

func TestReportExceedsThreshold(t *testing.T) {
	report := &checksapi.GoogleChecksReportV1alphaReport{
		Checks: []*checksapi.GoogleChecksReportV1alphaCheck{
			{State: "FAILED", Severity: "POTENTIAL"},
			{State: "PASSED", Severity: "PRIORITY"},
		},
	}
	if !reportExceedsThreshold(report, "OPPORTUNITY") {
		t.Fatal("expected failed POTENTIAL check to exceed OPPORTUNITY")
	}
	if !reportExceedsThreshold(report, "POTENTIAL") {
		t.Fatal("expected failed POTENTIAL check to exceed POTENTIAL")
	}
	if reportExceedsThreshold(report, "PRIORITY") {
		t.Fatal("did not expect failed POTENTIAL check to exceed PRIORITY")
	}
}

func TestClassificationViolates(t *testing.T) {
	resp := &checksapi.GoogleChecksAisafetyV1alphaClassifyContentResponse{
		PolicyResults: []*checksapi.GoogleChecksAisafetyV1alphaClassifyContentResponsePolicyResult{
			{ViolationResult: "NON_VIOLATIVE"},
			{ViolationResult: "VIOLATIVE"},
		},
	}
	if !classificationViolates(resp) {
		t.Fatal("expected VIOLATIVE result to fail classification threshold")
	}
}

func TestParsePolicies_All(t *testing.T) {
	policies, err := parsePolicies("all")
	if err != nil {
		t.Fatalf("parsePolicies returned error: %v", err)
	}
	if len(policies) != len(policyTypes) {
		t.Fatalf("got %d policies, want %d", len(policies), len(policyTypes))
	}
}

func TestParsePolicies_Invalid(t *testing.T) {
	_, err := parsePolicies("BAD_POLICY")
	if err == nil {
		t.Fatal("expected invalid policy error")
	}
}

func TestUploadCommandHasChecksFilter(t *testing.T) {
	for _, name := range []string{"upload", "analyze"} {
		if UploadCommand(name).FlagSet.Lookup("checks-filter") == nil {
			t.Fatalf("%q command should define --checks-filter", name)
		}
	}
}

func TestThresholdErrorIsStable(t *testing.T) {
	if !errors.Is(errThresholdExceeded, errThresholdExceeded) {
		t.Fatal("threshold sentinel should be comparable through errors.Is")
	}
}
