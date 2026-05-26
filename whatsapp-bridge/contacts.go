package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
)

// --- Request/response types ---

type GetUsersRequest struct {
	JIDs []string `json:"jids"`
}

type UserInfoDTO struct {
	JID          string   `json:"jid"`
	Status       string   `json:"status"`
	PictureID    string   `json:"picture_id"`
	LID          string   `json:"lid,omitempty"`
	Devices      []string `json:"devices"`
	VerifiedName string   `json:"verified_name,omitempty"`
}

type UserInfoResponse struct {
	Success bool          `json:"success"`
	Message string        `json:"message,omitempty"`
	Users   []UserInfoDTO `json:"users"`
}

type ProfilePictureRequest struct {
	JID     string `json:"jid"`
	Preview bool   `json:"preview"` // true = lower-res thumbnail
}

type ProfilePictureResponse struct {
	Success bool   `json:"success"`
	Message string `json:"message,omitempty"`
	URL     string `json:"url,omitempty"`
	ID      string `json:"id,omitempty"`
	Type    string `json:"type,omitempty"`
}

type JIDOnlyRequest struct {
	JID string `json:"jid"`
}

type BusinessProfileResponse struct {
	Success bool             `json:"success"`
	Message string           `json:"message,omitempty"`
	Profile *BusinessProfDTO `json:"profile,omitempty"`
}

type BusinessProfDTO struct {
	JID            string            `json:"jid"`
	Address        string            `json:"address,omitempty"`
	Email          string            `json:"email,omitempty"`
	Categories     []string          `json:"categories,omitempty"`
	ProfileOptions map[string]string `json:"profile_options,omitempty"`
	Timezone       string            `json:"timezone,omitempty"`
}

type BlocklistResponse struct {
	Success bool     `json:"success"`
	Message string   `json:"message,omitempty"`
	JIDs    []string `json:"jids"`
}

type BlockRequest struct {
	JID   string `json:"jid"`
	Block bool   `json:"block"`
}

type StatusMessageRequest struct {
	Message string `json:"message"`
}

type PrivacySettingRequest struct {
	SettingType string `json:"setting_type"` // e.g. "last", "profile", "status", "readreceipts"
	Value       string `json:"value"`        // e.g. "all", "contacts", "none"
}

type ResolveBusinessLinkRequest struct {
	Link string `json:"link"` // full URL or code
}

type ResolveBusinessLinkResponse struct {
	Success      bool   `json:"success"`
	Message      string `json:"message,omitempty"`
	JID          string `json:"jid,omitempty"`
	PushName     string `json:"push_name,omitempty"`
	VerifiedName string `json:"verified_name,omitempty"`
	Greeting     string `json:"greeting,omitempty"`
}

// --- HTTP handlers ---

func registerContactRoutes(client *whatsmeow.Client) {
	http.HandleFunc("/api/users/info", func(w http.ResponseWriter, r *http.Request) {
		writeJSON := newJSONWriter(w)
		if r.Method != http.MethodPost {
			writeJSON(http.StatusMethodNotAllowed, UserInfoResponse{Success: false, Message: "Method not allowed"})
			return
		}
		var req GetUsersRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil || len(req.JIDs) == 0 {
			writeJSON(http.StatusBadRequest, UserInfoResponse{Success: false, Message: "jids is required"})
			return
		}
		jids, err := parseJIDList(req.JIDs)
		if err != nil {
			writeJSON(http.StatusBadRequest, UserInfoResponse{Success: false, Message: err.Error()})
			return
		}
		infos, err := client.GetUserInfo(context.Background(), jids)
		if err != nil {
			writeJSON(http.StatusInternalServerError, UserInfoResponse{Success: false, Message: err.Error()})
			return
		}
		out := make([]UserInfoDTO, 0, len(infos))
		for jid, info := range infos {
			dto := UserInfoDTO{
				JID:       jid.String(),
				Status:    info.Status,
				PictureID: info.PictureID,
			}
			if !info.LID.IsEmpty() {
				dto.LID = info.LID.String()
			}
			for _, d := range info.Devices {
				dto.Devices = append(dto.Devices, d.String())
			}
			if info.VerifiedName != nil && info.VerifiedName.Details != nil && info.VerifiedName.Details.VerifiedName != nil {
				dto.VerifiedName = *info.VerifiedName.Details.VerifiedName
			}
			out = append(out, dto)
		}
		writeJSON(http.StatusOK, UserInfoResponse{Success: true, Users: out})
	})

	http.HandleFunc("/api/users/profile-picture", func(w http.ResponseWriter, r *http.Request) {
		writeJSON := newJSONWriter(w)
		if r.Method != http.MethodPost {
			writeJSON(http.StatusMethodNotAllowed, ProfilePictureResponse{Success: false, Message: "Method not allowed"})
			return
		}
		var req ProfilePictureRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.JID == "" {
			writeJSON(http.StatusBadRequest, ProfilePictureResponse{Success: false, Message: "jid is required"})
			return
		}
		jid, err := types.ParseJID(req.JID)
		if err != nil {
			writeJSON(http.StatusBadRequest, ProfilePictureResponse{Success: false, Message: err.Error()})
			return
		}
		info, err := client.GetProfilePictureInfo(context.Background(), jid, &whatsmeow.GetProfilePictureParams{Preview: req.Preview})
		if err != nil {
			writeJSON(http.StatusInternalServerError, ProfilePictureResponse{Success: false, Message: err.Error()})
			return
		}
		if info == nil {
			writeJSON(http.StatusOK, ProfilePictureResponse{Success: true, Message: "No profile picture available"})
			return
		}
		writeJSON(http.StatusOK, ProfilePictureResponse{
			Success: true,
			URL:     info.URL,
			ID:      info.ID,
			Type:    info.Type,
		})
	})

	http.HandleFunc("/api/users/business-profile", func(w http.ResponseWriter, r *http.Request) {
		writeJSON := newJSONWriter(w)
		if r.Method != http.MethodPost {
			writeJSON(http.StatusMethodNotAllowed, BusinessProfileResponse{Success: false, Message: "Method not allowed"})
			return
		}
		var req JIDOnlyRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.JID == "" {
			writeJSON(http.StatusBadRequest, BusinessProfileResponse{Success: false, Message: "jid is required"})
			return
		}
		jid, err := types.ParseJID(req.JID)
		if err != nil {
			writeJSON(http.StatusBadRequest, BusinessProfileResponse{Success: false, Message: err.Error()})
			return
		}
		profile, err := client.GetBusinessProfile(context.Background(), jid)
		if err != nil {
			writeJSON(http.StatusInternalServerError, BusinessProfileResponse{Success: false, Message: err.Error()})
			return
		}
		if profile == nil {
			writeJSON(http.StatusOK, BusinessProfileResponse{Success: true, Message: "Not a business account"})
			return
		}
		dto := &BusinessProfDTO{
			JID:            profile.JID.String(),
			Address:        profile.Address,
			Email:          profile.Email,
			ProfileOptions: profile.ProfileOptions,
			Timezone:       profile.BusinessHoursTimeZone,
		}
		for _, c := range profile.Categories {
			dto.Categories = append(dto.Categories, c.Name)
		}
		writeJSON(http.StatusOK, BusinessProfileResponse{Success: true, Profile: dto})
	})

	http.HandleFunc("/api/users/blocklist", func(w http.ResponseWriter, r *http.Request) {
		writeJSON := newJSONWriter(w)
		if r.Method != http.MethodGet {
			writeJSON(http.StatusMethodNotAllowed, BlocklistResponse{Success: false, Message: "Method not allowed"})
			return
		}
		bl, err := client.GetBlocklist(context.Background())
		if err != nil {
			writeJSON(http.StatusInternalServerError, BlocklistResponse{Success: false, Message: err.Error()})
			return
		}
		jids := make([]string, 0, len(bl.JIDs))
		for _, j := range bl.JIDs {
			jids = append(jids, j.String())
		}
		writeJSON(http.StatusOK, BlocklistResponse{Success: true, JIDs: jids})
	})

	http.HandleFunc("/api/users/block", func(w http.ResponseWriter, r *http.Request) {
		writeJSON := newJSONWriter(w)
		if r.Method != http.MethodPost {
			writeJSON(http.StatusMethodNotAllowed, GenericResponse{Success: false, Message: "Method not allowed"})
			return
		}
		var req BlockRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.JID == "" {
			writeJSON(http.StatusBadRequest, GenericResponse{Success: false, Message: "jid is required"})
			return
		}
		jid, err := types.ParseJID(req.JID)
		if err != nil {
			writeJSON(http.StatusBadRequest, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		action := events.BlocklistChangeActionUnblock
		if req.Block {
			action = events.BlocklistChangeActionBlock
		}
		if _, err := client.UpdateBlocklist(context.Background(), jid, action); err != nil {
			writeJSON(http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		verb := "Unblocked"
		if req.Block {
			verb = "Blocked"
		}
		writeJSON(http.StatusOK, GenericResponse{Success: true, Message: fmt.Sprintf("%s %s", verb, req.JID)})
	})

	http.HandleFunc("/api/users/status-message", func(w http.ResponseWriter, r *http.Request) {
		writeJSON := newJSONWriter(w)
		if r.Method != http.MethodPost {
			writeJSON(http.StatusMethodNotAllowed, GenericResponse{Success: false, Message: "Method not allowed"})
			return
		}
		var req StatusMessageRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			writeJSON(http.StatusBadRequest, GenericResponse{Success: false, Message: "Invalid request format"})
			return
		}
		if err := client.SetStatusMessage(context.Background(), req.Message); err != nil {
			writeJSON(http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		writeJSON(http.StatusOK, GenericResponse{Success: true, Message: "Status updated"})
	})

	http.HandleFunc("/api/users/privacy", func(w http.ResponseWriter, r *http.Request) {
		writeJSON := newJSONWriter(w)
		if r.Method != http.MethodPost {
			writeJSON(http.StatusMethodNotAllowed, GenericResponse{Success: false, Message: "Method not allowed"})
			return
		}
		var req PrivacySettingRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.SettingType == "" || req.Value == "" {
			writeJSON(http.StatusBadRequest, GenericResponse{Success: false, Message: "setting_type and value are required"})
			return
		}
		if _, err := client.SetPrivacySetting(
			context.Background(),
			types.PrivacySettingType(req.SettingType),
			types.PrivacySetting(req.Value),
		); err != nil {
			writeJSON(http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		writeJSON(http.StatusOK, GenericResponse{Success: true, Message: fmt.Sprintf("Privacy setting %s=%s saved", req.SettingType, req.Value)})
	})

	http.HandleFunc("/api/users/resolve-business-link", func(w http.ResponseWriter, r *http.Request) {
		writeJSON := newJSONWriter(w)
		if r.Method != http.MethodPost {
			writeJSON(http.StatusMethodNotAllowed, ResolveBusinessLinkResponse{Success: false, Message: "Method not allowed"})
			return
		}
		var req ResolveBusinessLinkRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.Link == "" {
			writeJSON(http.StatusBadRequest, ResolveBusinessLinkResponse{Success: false, Message: "link is required"})
			return
		}
		code := extractInviteCode(req.Link)
		target, err := client.ResolveBusinessMessageLink(context.Background(), code)
		if err != nil {
			writeJSON(http.StatusInternalServerError, ResolveBusinessLinkResponse{Success: false, Message: err.Error()})
			return
		}
		writeJSON(http.StatusOK, ResolveBusinessLinkResponse{
			Success:      true,
			JID:          target.JID.String(),
			PushName:     target.PushName,
			VerifiedName: target.VerifiedName,
			Greeting:     target.Message,
		})
	})
}
