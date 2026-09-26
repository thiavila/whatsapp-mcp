package main

import (
	"context"
	"errors"
	"testing"

	"go.mau.fi/whatsmeow/types"
)

// fakeLookup answers like the real server did in the 2026-09-26 probe: numbers are
// normalised (with or without the Brazilian 9), unknown numbers come back IsIn=false,
// and numbers it can't parse are dropped from the response.
func fakeLookup(known map[string]types.IsOnWhatsAppResponse) phoneLookup {
	return func(_ context.Context, phones []string) ([]types.IsOnWhatsAppResponse, error) {
		var out []types.IsOnWhatsAppResponse
		for _, p := range phones {
			if r, ok := known[p]; ok {
				r.Query = p
				out = append(out, r)
			}
		}
		return out, nil
	}
}

var elis = types.IsOnWhatsAppResponse{
	IsIn:        true,
	JID:         types.NewJID("133282012393719", types.HiddenUserServer),
	PhoneNumber: types.NewJID("558196968434", types.DefaultUserServer),
}

func TestResolveRecipientUsesCanonicalNumberWithoutThe9(t *testing.T) {
	lookup := fakeLookup(map[string]types.IsOnWhatsAppResponse{
		"+5581996968434": elis,
		"+558196968434":  elis,
	})
	for _, in := range []string{"5581996968434", "+55 81 99696-8434", "558196968434"} {
		jid, err := resolveRecipient(context.Background(), lookup, in)
		if err != nil {
			t.Fatalf("%s: %v", in, err)
		}
		if got, want := jid.String(), "558196968434@s.whatsapp.net"; got != want {
			t.Fatalf("%s: got %s, want %s", in, got, want)
		}
	}
}

func TestResolveRecipientFallsBackToLIDWithoutPhoneNumber(t *testing.T) {
	lookup := fakeLookup(map[string]types.IsOnWhatsAppResponse{
		"+551120594300": {IsIn: true, JID: types.NewJID("256590540206310", types.HiddenUserServer)},
	})
	jid, err := resolveRecipient(context.Background(), lookup, "551120594300")
	if err != nil {
		t.Fatal(err)
	}
	if got, want := jid.String(), "256590540206310@lid"; got != want {
		t.Fatalf("got %s, want %s", got, want)
	}
}

func TestResolveRecipientRejectsNumbersNotOnWhatsApp(t *testing.T) {
	lookup := fakeLookup(map[string]types.IsOnWhatsAppResponse{
		"+5511900000001": {IsIn: false, JID: types.NewJID("5511900000001", types.DefaultUserServer)},
	})
	for _, in := range []string{"5511900000001", "551110011717" /* dropped by the server */} {
		if _, err := resolveRecipient(context.Background(), lookup, in); !errors.Is(err, errNotOnWhatsApp) {
			t.Fatalf("%s: err = %v, want errNotOnWhatsApp", in, err)
		}
	}
}

func TestResolveRecipientFallsBackWhenLookupFails(t *testing.T) {
	lookup := func(context.Context, []string) ([]types.IsOnWhatsAppResponse, error) {
		return nil, errors.New("timeout")
	}
	jid, err := resolveRecipient(context.Background(), lookup, "5581996968434")
	if err != nil {
		t.Fatal(err)
	}
	if got, want := jid.String(), "5581996968434@s.whatsapp.net"; got != want {
		t.Fatalf("got %s, want %s", got, want)
	}
}

func TestResolveRecipientKeepsExplicitJIDsWithoutLookup(t *testing.T) {
	lookup := func(context.Context, []string) ([]types.IsOnWhatsAppResponse, error) {
		t.Fatal("explicit JIDs must not hit the server")
		return nil, nil
	}
	for _, in := range []string{"133282012393719@lid", "120363372919264613@g.us"} {
		jid, err := resolveRecipient(context.Background(), lookup, in)
		if err != nil || jid.String() != in {
			t.Fatalf("%s: got %s, %v", in, jid, err)
		}
	}
}

func TestResolveRecipientsSkipsBlanksAndStopsOnError(t *testing.T) {
	lookup := fakeLookup(map[string]types.IsOnWhatsAppResponse{"+5581996968434": elis})
	jids, err := resolveRecipients(context.Background(), lookup, []string{"5581996968434", " ", "120363372919264613@g.us"})
	if err != nil || len(jids) != 2 {
		t.Fatalf("got %v, %v", jids, err)
	}
	if _, err := resolveRecipients(context.Background(), lookup, []string{"5581996968434", "5511900000001"}); err == nil {
		t.Fatal("expected error for unknown number")
	}
}
