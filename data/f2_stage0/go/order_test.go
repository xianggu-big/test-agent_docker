package shop

import "testing"

func TestGetTier(t *testing.T) {
	if GetTier(95) != "A" {
		t.Fatalf("95 want A got %s", GetTier(95))
	}
	if GetTier(90) != "A" {
		t.Fatalf("90 want A got %s", GetTier(90))
	}
}

func TestAdd(t *testing.T) {
	if Add(1, 2) != 3 {
		t.Fatal("add wrong")
	}
}
