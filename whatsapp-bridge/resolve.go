package main

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"time"

	"go.mau.fi/whatsmeow/types"
)

// phoneLookup is client.IsOnWhatsApp; injected so the resolution logic is testable.
type phoneLookup func(ctx context.Context, phones []string) ([]types.IsOnWhatsAppResponse, error)

var errNotOnWhatsApp = errors.New("number is not on WhatsApp")

const phoneLookupTimeout = 10 * time.Second

// resolveRecipient turns a JID or a phone number into the JID WhatsApp actually uses.
//
// Phone numbers can't be turned into a JID by just appending @s.whatsapp.net: Brazilian
// mobiles registered before the 9th digit keep the old 8-digit JID (55 81 99696-8434 lives
// at 558196968434@s.whatsapp.net), so the naive JID has no LID and SendMessage fails with
// "no LID found". The server normalises the number in the IsOnWhatsApp query (with or
// without the 9, landlines too), so we ask it and use the canonical phone JID it returns.
// That query also stores the PN↔LID mapping, which SendMessage needs next.
//
// If the lookup itself fails (network, timeout) we fall back to the naive JID, i.e. the
// old behaviour.
func resolveRecipient(ctx context.Context, lookup phoneLookup, recipient string) (types.JID, error) {
	recipient = strings.TrimSpace(recipient)
	if strings.Contains(recipient, "@") {
		return parseRecipientJID(recipient)
	}
	// Only phone formatting is tolerated ("+", spaces, dashes, dots, parentheses).
	// Anything else (letters, an extension like "x8") is refused instead of being
	// glued onto the digits, which could produce a different, real number.
	if strings.TrimLeft(recipient, "+0123456789 -.()") != "" || strings.Count(recipient, "+") > 1 ||
		(strings.Contains(recipient, "+") && !strings.HasPrefix(recipient, "+")) {
		return types.EmptyJID, fmt.Errorf("invalid phone number %q: only digits, spaces, dashes, dots, parentheses and a leading + are allowed", recipient)
	}
	digits := onlyDigits(recipient)
	if len(digits) < 8 {
		return types.EmptyJID, fmt.Errorf("invalid phone number %q", recipient)
	}

	ctx, cancel := context.WithTimeout(ctx, phoneLookupTimeout)
	defer cancel()
	results, err := lookup(ctx, []string{"+" + digits})
	if err != nil {
		fmt.Printf("Phone lookup for %s failed, using the number as-is: %v\n", digits, err)
		return types.NewJID(digits, types.DefaultUserServer), nil
	}
	for _, r := range results {
		if !r.IsIn {
			continue
		}
		if !r.PhoneNumber.IsEmpty() {
			return r.PhoneNumber.ToNonAD(), nil
		}
		if !r.JID.IsEmpty() {
			return r.JID.ToNonAD(), nil
		}
	}
	if len(results) > 0 {
		return types.EmptyJID, fmt.Errorf("%w: %s", errNotOnWhatsApp, digits)
	}
	// The server drops queries it can't parse as a phone number (e.g. a subscriber
	// number starting with 1).
	return types.EmptyJID, fmt.Errorf("%w: %s (not recognised as a phone number)", errNotOnWhatsApp, digits)
}

// resolveRecipients resolves each entry independently: the server merges duplicate
// people in one batch (with and without the 9), which would leave queries unanswered.
func resolveRecipients(ctx context.Context, lookup phoneLookup, raw []string) ([]types.JID, error) {
	out := make([]types.JID, 0, len(raw))
	for _, s := range raw {
		if strings.TrimSpace(s) == "" {
			continue
		}
		jid, err := resolveRecipient(ctx, lookup, s)
		if err != nil {
			return nil, err
		}
		out = append(out, jid)
	}
	return out, nil
}

func onlyDigits(s string) string {
	var b strings.Builder
	for _, r := range s {
		if r >= '0' && r <= '9' {
			b.WriteRune(r)
		}
	}
	return b.String()
}
