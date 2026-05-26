package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"strings"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/types"
)

// --- Request/response types ---

type CreateGroupRequest struct {
	Name string `json:"name"`
	// Participant JIDs (or phone numbers, which the bridge canonicalises).
	Participants []string `json:"participants"`
}

type CreateGroupResponse struct {
	Success  bool          `json:"success"`
	Message  string        `json:"message"`
	GroupJID string        `json:"group_jid,omitempty"`
	Info     *GroupInfoDTO `json:"info,omitempty"`
}

type GroupJIDRequest struct {
	ChatJID string `json:"chat_jid"`
}

type GroupInviteLinkRequest struct {
	ChatJID string `json:"chat_jid"`
	Reset   bool   `json:"reset"`
}

type GroupInviteLinkResponse struct {
	Success bool   `json:"success"`
	Message string `json:"message,omitempty"`
	Link    string `json:"link"`
}

type GroupLinkRequest struct {
	// Either the full https://chat.whatsapp.com/<code> URL or just the code.
	Link string `json:"link"`
}

type JoinGroupResponse struct {
	Success  bool   `json:"success"`
	Message  string `json:"message"`
	GroupJID string `json:"group_jid,omitempty"`
}

type UpdateParticipantsRequest struct {
	ChatJID      string   `json:"chat_jid"`
	Participants []string `json:"participants"`
	Action       string   `json:"action"` // add, remove, promote, demote
}

type SetGroupNameRequest struct {
	ChatJID string `json:"chat_jid"`
	Name    string `json:"name"`
}

type SetGroupDescriptionRequest struct {
	ChatJID     string `json:"chat_jid"`
	Description string `json:"description"`
}

type SetGroupPhotoRequest struct {
	ChatJID string `json:"chat_jid"`
	// Local file path; empty means remove the current photo.
	PhotoPath string `json:"photo_path"`
}

type SetGroupPhotoResponse struct {
	Success   bool   `json:"success"`
	Message   string `json:"message"`
	PictureID string `json:"picture_id,omitempty"`
}

type SetGroupBoolRequest struct {
	ChatJID string `json:"chat_jid"`
	Value   bool   `json:"value"`
}

type GroupRequestsResponse struct {
	Success  bool                       `json:"success"`
	Message  string                     `json:"message,omitempty"`
	Requests []GroupParticipantRequestDTO `json:"requests"`
}

type GroupParticipantRequestDTO struct {
	JID         string `json:"jid"`
	RequestedAt string `json:"requested_at"`
}

type DecideRequestsRequest struct {
	ChatJID      string   `json:"chat_jid"`
	Participants []string `json:"participants"`
	Approve      bool     `json:"approve"`
}

type ListGroupsResponse struct {
	Success bool           `json:"success"`
	Message string         `json:"message,omitempty"`
	Groups  []GroupInfoDTO `json:"groups"`
}

type GroupInfoResponse struct {
	Success bool          `json:"success"`
	Message string        `json:"message,omitempty"`
	Group   *GroupInfoDTO `json:"group,omitempty"`
}

// GroupInfoDTO is a JSON-serialisable projection of types.GroupInfo. We don't
// expose the raw whatsmeow type because it carries proto/internal fields that
// don't marshal cleanly.
type GroupInfoDTO struct {
	JID                    string                  `json:"jid"`
	Name                   string                  `json:"name"`
	Topic                  string                  `json:"topic,omitempty"`
	OwnerJID               string                  `json:"owner_jid,omitempty"`
	GroupCreated           string                  `json:"group_created,omitempty"`
	ParticipantCount       int                     `json:"participant_count"`
	IsAnnounce             bool                    `json:"is_announce"`
	IsLocked               bool                    `json:"is_locked"`
	IsEphemeral            bool                    `json:"is_ephemeral"`
	IsJoinApprovalRequired bool                    `json:"is_join_approval_required"`
	IsParent               bool                    `json:"is_parent"`
	LinkedParentJID        string                  `json:"linked_parent_jid,omitempty"`
	Participants           []GroupParticipantDTO   `json:"participants"`
}

type GroupParticipantDTO struct {
	JID          string `json:"jid"`
	PhoneNumber  string `json:"phone_number,omitempty"`
	IsAdmin      bool   `json:"is_admin"`
	IsSuperAdmin bool   `json:"is_super_admin"`
	DisplayName  string `json:"display_name,omitempty"`
}

// --- Helpers ---

func groupInfoToDTO(g *types.GroupInfo) *GroupInfoDTO {
	if g == nil {
		return nil
	}
	dto := &GroupInfoDTO{
		JID:                    g.JID.String(),
		Name:                   g.Name,
		Topic:                  g.Topic,
		ParticipantCount:       g.ParticipantCount,
		IsAnnounce:             g.IsAnnounce,
		IsLocked:               g.IsLocked,
		IsEphemeral:            g.IsEphemeral,
		IsJoinApprovalRequired: g.IsJoinApprovalRequired,
		IsParent:               g.IsParent,
	}
	if !g.OwnerJID.IsEmpty() {
		dto.OwnerJID = g.OwnerJID.String()
	}
	if !g.LinkedParentJID.IsEmpty() {
		dto.LinkedParentJID = g.LinkedParentJID.String()
	}
	if !g.GroupCreated.IsZero() {
		dto.GroupCreated = g.GroupCreated.Format("2006-01-02T15:04:05Z07:00")
	}
	dto.Participants = make([]GroupParticipantDTO, 0, len(g.Participants))
	for _, p := range g.Participants {
		entry := GroupParticipantDTO{
			JID:          p.JID.String(),
			IsAdmin:      p.IsAdmin,
			IsSuperAdmin: p.IsSuperAdmin,
			DisplayName:  p.DisplayName,
		}
		if !p.PhoneNumber.IsEmpty() {
			entry.PhoneNumber = p.PhoneNumber.String()
		}
		dto.Participants = append(dto.Participants, entry)
	}
	return dto
}

// parseJIDList canonicalises a list of JID-or-phone-number strings.
func parseJIDList(raw []string) ([]types.JID, error) {
	out := make([]types.JID, 0, len(raw))
	for _, s := range raw {
		s = strings.TrimSpace(s)
		if s == "" {
			continue
		}
		if strings.Contains(s, "@") {
			j, err := types.ParseJID(s)
			if err != nil {
				return nil, fmt.Errorf("invalid JID %q: %v", s, err)
			}
			out = append(out, j)
		} else {
			// Strip a leading "+" if present so types.JID validates.
			s = strings.TrimPrefix(s, "+")
			out = append(out, types.JID{User: s, Server: types.DefaultUserServer})
		}
	}
	return out, nil
}

// extractInviteCode pulls the trailing code from a wa.me/chat.whatsapp.com URL.
func extractInviteCode(link string) string {
	link = strings.TrimSpace(link)
	if link == "" {
		return ""
	}
	// Common forms: https://chat.whatsapp.com/<code>, https://wa.me/<code>, or just <code>.
	if i := strings.LastIndex(link, "/"); i >= 0 {
		return strings.TrimSpace(link[i+1:])
	}
	return link
}

// --- HTTP handlers ---

func registerGroupRoutes(client *whatsmeow.Client) {
	http.HandleFunc("/api/groups/create", func(w http.ResponseWriter, r *http.Request) {
		writeJSON := newJSONWriter(w)
		if r.Method != http.MethodPost {
			writeJSON(http.StatusMethodNotAllowed, CreateGroupResponse{Success: false, Message: "Method not allowed"})
			return
		}
		var req CreateGroupRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			writeJSON(http.StatusBadRequest, CreateGroupResponse{Success: false, Message: "Invalid request format"})
			return
		}
		if req.Name == "" {
			writeJSON(http.StatusBadRequest, CreateGroupResponse{Success: false, Message: "name is required"})
			return
		}
		participants, err := parseJIDList(req.Participants)
		if err != nil {
			writeJSON(http.StatusBadRequest, CreateGroupResponse{Success: false, Message: err.Error()})
			return
		}
		info, err := client.CreateGroup(context.Background(), whatsmeow.ReqCreateGroup{
			Name:         req.Name,
			Participants: participants,
		})
		if err != nil {
			writeJSON(http.StatusInternalServerError, CreateGroupResponse{Success: false, Message: err.Error()})
			return
		}
		dto := groupInfoToDTO(info)
		writeJSON(http.StatusOK, CreateGroupResponse{
			Success:  true,
			Message:  "Group created",
			GroupJID: dto.JID,
			Info:     dto,
		})
	})

	http.HandleFunc("/api/groups/leave", func(w http.ResponseWriter, r *http.Request) {
		writeJSON := newJSONWriter(w)
		if r.Method != http.MethodPost {
			writeJSON(http.StatusMethodNotAllowed, GenericResponse{Success: false, Message: "Method not allowed"})
			return
		}
		var req GroupJIDRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.ChatJID == "" {
			writeJSON(http.StatusBadRequest, GenericResponse{Success: false, Message: "chat_jid is required"})
			return
		}
		chatJID, err := types.ParseJID(req.ChatJID)
		if err != nil {
			writeJSON(http.StatusBadRequest, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		if err := client.LeaveGroup(context.Background(), chatJID); err != nil {
			writeJSON(http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		writeJSON(http.StatusOK, GenericResponse{Success: true, Message: "Left group"})
	})

	http.HandleFunc("/api/groups/info", func(w http.ResponseWriter, r *http.Request) {
		writeJSON := newJSONWriter(w)
		if r.Method != http.MethodGet {
			writeJSON(http.StatusMethodNotAllowed, GroupInfoResponse{Success: false, Message: "Method not allowed"})
			return
		}
		jidStr := r.URL.Query().Get("chat_jid")
		if jidStr == "" {
			writeJSON(http.StatusBadRequest, GroupInfoResponse{Success: false, Message: "chat_jid is required"})
			return
		}
		jid, err := types.ParseJID(jidStr)
		if err != nil {
			writeJSON(http.StatusBadRequest, GroupInfoResponse{Success: false, Message: err.Error()})
			return
		}
		info, err := client.GetGroupInfo(context.Background(), jid)
		if err != nil {
			writeJSON(http.StatusInternalServerError, GroupInfoResponse{Success: false, Message: err.Error()})
			return
		}
		writeJSON(http.StatusOK, GroupInfoResponse{Success: true, Group: groupInfoToDTO(info)})
	})

	http.HandleFunc("/api/groups/list", func(w http.ResponseWriter, r *http.Request) {
		writeJSON := newJSONWriter(w)
		if r.Method != http.MethodGet {
			writeJSON(http.StatusMethodNotAllowed, ListGroupsResponse{Success: false, Message: "Method not allowed"})
			return
		}
		groups, err := client.GetJoinedGroups(context.Background())
		if err != nil {
			writeJSON(http.StatusInternalServerError, ListGroupsResponse{Success: false, Message: err.Error()})
			return
		}
		out := make([]GroupInfoDTO, 0, len(groups))
		for _, g := range groups {
			if dto := groupInfoToDTO(g); dto != nil {
				out = append(out, *dto)
			}
		}
		writeJSON(http.StatusOK, ListGroupsResponse{Success: true, Groups: out})
	})

	http.HandleFunc("/api/groups/invite-link", func(w http.ResponseWriter, r *http.Request) {
		writeJSON := newJSONWriter(w)
		if r.Method != http.MethodPost {
			writeJSON(http.StatusMethodNotAllowed, GroupInviteLinkResponse{Success: false, Message: "Method not allowed"})
			return
		}
		var req GroupInviteLinkRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.ChatJID == "" {
			writeJSON(http.StatusBadRequest, GroupInviteLinkResponse{Success: false, Message: "chat_jid is required"})
			return
		}
		jid, err := types.ParseJID(req.ChatJID)
		if err != nil {
			writeJSON(http.StatusBadRequest, GroupInviteLinkResponse{Success: false, Message: err.Error()})
			return
		}
		code, err := client.GetGroupInviteLink(context.Background(), jid, req.Reset)
		if err != nil {
			writeJSON(http.StatusInternalServerError, GroupInviteLinkResponse{Success: false, Message: err.Error()})
			return
		}
		writeJSON(http.StatusOK, GroupInviteLinkResponse{
			Success: true,
			Link:    "https://chat.whatsapp.com/" + code,
		})
	})

	http.HandleFunc("/api/groups/info-from-link", func(w http.ResponseWriter, r *http.Request) {
		writeJSON := newJSONWriter(w)
		if r.Method != http.MethodPost {
			writeJSON(http.StatusMethodNotAllowed, GroupInfoResponse{Success: false, Message: "Method not allowed"})
			return
		}
		var req GroupLinkRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.Link == "" {
			writeJSON(http.StatusBadRequest, GroupInfoResponse{Success: false, Message: "link is required"})
			return
		}
		code := extractInviteCode(req.Link)
		info, err := client.GetGroupInfoFromLink(context.Background(), code)
		if err != nil {
			writeJSON(http.StatusInternalServerError, GroupInfoResponse{Success: false, Message: err.Error()})
			return
		}
		writeJSON(http.StatusOK, GroupInfoResponse{Success: true, Group: groupInfoToDTO(info)})
	})

	http.HandleFunc("/api/groups/join", func(w http.ResponseWriter, r *http.Request) {
		writeJSON := newJSONWriter(w)
		if r.Method != http.MethodPost {
			writeJSON(http.StatusMethodNotAllowed, JoinGroupResponse{Success: false, Message: "Method not allowed"})
			return
		}
		var req GroupLinkRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.Link == "" {
			writeJSON(http.StatusBadRequest, JoinGroupResponse{Success: false, Message: "link is required"})
			return
		}
		code := extractInviteCode(req.Link)
		jid, err := client.JoinGroupWithLink(context.Background(), code)
		if err != nil {
			writeJSON(http.StatusInternalServerError, JoinGroupResponse{Success: false, Message: err.Error()})
			return
		}
		writeJSON(http.StatusOK, JoinGroupResponse{Success: true, Message: "Joined group", GroupJID: jid.String()})
	})

	http.HandleFunc("/api/groups/participants", func(w http.ResponseWriter, r *http.Request) {
		writeJSON := newJSONWriter(w)
		if r.Method != http.MethodPost {
			writeJSON(http.StatusMethodNotAllowed, GenericResponse{Success: false, Message: "Method not allowed"})
			return
		}
		var req UpdateParticipantsRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			writeJSON(http.StatusBadRequest, GenericResponse{Success: false, Message: "Invalid request format"})
			return
		}
		if req.ChatJID == "" || len(req.Participants) == 0 {
			writeJSON(http.StatusBadRequest, GenericResponse{Success: false, Message: "chat_jid and participants are required"})
			return
		}
		var action whatsmeow.ParticipantChange
		switch req.Action {
		case "add":
			action = whatsmeow.ParticipantChangeAdd
		case "remove":
			action = whatsmeow.ParticipantChangeRemove
		case "promote":
			action = whatsmeow.ParticipantChangePromote
		case "demote":
			action = whatsmeow.ParticipantChangeDemote
		default:
			writeJSON(http.StatusBadRequest, GenericResponse{Success: false, Message: "action must be add, remove, promote or demote"})
			return
		}
		chatJID, err := types.ParseJID(req.ChatJID)
		if err != nil {
			writeJSON(http.StatusBadRequest, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		participants, err := parseJIDList(req.Participants)
		if err != nil {
			writeJSON(http.StatusBadRequest, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		if _, err := client.UpdateGroupParticipants(context.Background(), chatJID, participants, action); err != nil {
			writeJSON(http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		writeJSON(http.StatusOK, GenericResponse{Success: true, Message: fmt.Sprintf("Participants %s", req.Action)})
	})

	http.HandleFunc("/api/groups/name", func(w http.ResponseWriter, r *http.Request) {
		writeJSON := newJSONWriter(w)
		if r.Method != http.MethodPost {
			writeJSON(http.StatusMethodNotAllowed, GenericResponse{Success: false, Message: "Method not allowed"})
			return
		}
		var req SetGroupNameRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.ChatJID == "" {
			writeJSON(http.StatusBadRequest, GenericResponse{Success: false, Message: "chat_jid is required"})
			return
		}
		jid, err := types.ParseJID(req.ChatJID)
		if err != nil {
			writeJSON(http.StatusBadRequest, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		if err := client.SetGroupName(context.Background(), jid, req.Name); err != nil {
			writeJSON(http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		writeJSON(http.StatusOK, GenericResponse{Success: true, Message: "Group name updated"})
	})

	http.HandleFunc("/api/groups/description", func(w http.ResponseWriter, r *http.Request) {
		writeJSON := newJSONWriter(w)
		if r.Method != http.MethodPost {
			writeJSON(http.StatusMethodNotAllowed, GenericResponse{Success: false, Message: "Method not allowed"})
			return
		}
		var req SetGroupDescriptionRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.ChatJID == "" {
			writeJSON(http.StatusBadRequest, GenericResponse{Success: false, Message: "chat_jid is required"})
			return
		}
		jid, err := types.ParseJID(req.ChatJID)
		if err != nil {
			writeJSON(http.StatusBadRequest, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		if err := client.SetGroupDescription(context.Background(), jid, req.Description); err != nil {
			writeJSON(http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		writeJSON(http.StatusOK, GenericResponse{Success: true, Message: "Group description updated"})
	})

	http.HandleFunc("/api/groups/photo", func(w http.ResponseWriter, r *http.Request) {
		writeJSON := newJSONWriter(w)
		if r.Method != http.MethodPost {
			writeJSON(http.StatusMethodNotAllowed, SetGroupPhotoResponse{Success: false, Message: "Method not allowed"})
			return
		}
		var req SetGroupPhotoRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.ChatJID == "" {
			writeJSON(http.StatusBadRequest, SetGroupPhotoResponse{Success: false, Message: "chat_jid is required"})
			return
		}
		jid, err := types.ParseJID(req.ChatJID)
		if err != nil {
			writeJSON(http.StatusBadRequest, SetGroupPhotoResponse{Success: false, Message: err.Error()})
			return
		}
		var photoBytes []byte
		if req.PhotoPath != "" {
			photoBytes, err = os.ReadFile(req.PhotoPath)
			if err != nil {
				writeJSON(http.StatusBadRequest, SetGroupPhotoResponse{Success: false, Message: fmt.Sprintf("Cannot read photo: %v", err)})
				return
			}
		}
		pid, err := client.SetGroupPhoto(context.Background(), jid, photoBytes)
		if err != nil {
			writeJSON(http.StatusInternalServerError, SetGroupPhotoResponse{Success: false, Message: err.Error()})
			return
		}
		msg := "Group photo updated"
		if req.PhotoPath == "" {
			msg = "Group photo removed"
		}
		writeJSON(http.StatusOK, SetGroupPhotoResponse{Success: true, Message: msg, PictureID: pid})
	})

	http.HandleFunc("/api/groups/announce", func(w http.ResponseWriter, r *http.Request) {
		setGroupBool(w, r, "announce", func(jid types.JID, v bool) error {
			return client.SetGroupAnnounce(context.Background(), jid, v)
		})
	})

	http.HandleFunc("/api/groups/locked", func(w http.ResponseWriter, r *http.Request) {
		setGroupBool(w, r, "locked", func(jid types.JID, v bool) error {
			return client.SetGroupLocked(context.Background(), jid, v)
		})
	})

	http.HandleFunc("/api/groups/approval-mode", func(w http.ResponseWriter, r *http.Request) {
		setGroupBool(w, r, "approval mode", func(jid types.JID, v bool) error {
			return client.SetGroupJoinApprovalMode(context.Background(), jid, v)
		})
	})

	http.HandleFunc("/api/groups/requests", func(w http.ResponseWriter, r *http.Request) {
		writeJSON := newJSONWriter(w)
		if r.Method != http.MethodGet {
			writeJSON(http.StatusMethodNotAllowed, GroupRequestsResponse{Success: false, Message: "Method not allowed"})
			return
		}
		jidStr := r.URL.Query().Get("chat_jid")
		if jidStr == "" {
			writeJSON(http.StatusBadRequest, GroupRequestsResponse{Success: false, Message: "chat_jid is required"})
			return
		}
		jid, err := types.ParseJID(jidStr)
		if err != nil {
			writeJSON(http.StatusBadRequest, GroupRequestsResponse{Success: false, Message: err.Error()})
			return
		}
		reqs, err := client.GetGroupRequestParticipants(context.Background(), jid)
		if err != nil {
			writeJSON(http.StatusInternalServerError, GroupRequestsResponse{Success: false, Message: err.Error()})
			return
		}
		out := make([]GroupParticipantRequestDTO, 0, len(reqs))
		for _, p := range reqs {
			out = append(out, GroupParticipantRequestDTO{
				JID:         p.JID.String(),
				RequestedAt: p.RequestedAt.Format("2006-01-02T15:04:05Z07:00"),
			})
		}
		writeJSON(http.StatusOK, GroupRequestsResponse{Success: true, Requests: out})
	})

	http.HandleFunc("/api/groups/requests/decide", func(w http.ResponseWriter, r *http.Request) {
		writeJSON := newJSONWriter(w)
		if r.Method != http.MethodPost {
			writeJSON(http.StatusMethodNotAllowed, GenericResponse{Success: false, Message: "Method not allowed"})
			return
		}
		var req DecideRequestsRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.ChatJID == "" || len(req.Participants) == 0 {
			writeJSON(http.StatusBadRequest, GenericResponse{Success: false, Message: "chat_jid and participants are required"})
			return
		}
		jid, err := types.ParseJID(req.ChatJID)
		if err != nil {
			writeJSON(http.StatusBadRequest, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		participants, err := parseJIDList(req.Participants)
		if err != nil {
			writeJSON(http.StatusBadRequest, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		action := whatsmeow.ParticipantChangeReject
		if req.Approve {
			action = whatsmeow.ParticipantChangeApprove
		}
		if _, err := client.UpdateGroupRequestParticipants(context.Background(), jid, participants, action); err != nil {
			writeJSON(http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		verb := "rejected"
		if req.Approve {
			verb = "approved"
		}
		writeJSON(http.StatusOK, GenericResponse{Success: true, Message: fmt.Sprintf("%d join request(s) %s", len(req.Participants), verb)})
	})
}

// setGroupBool factors out the boilerplate for the trio of bool-setting endpoints
// (announce / locked / approval-mode) that share the exact same request shape.
func setGroupBool(w http.ResponseWriter, r *http.Request, label string, apply func(types.JID, bool) error) {
	writeJSON := newJSONWriter(w)
	if r.Method != http.MethodPost {
		writeJSON(http.StatusMethodNotAllowed, GenericResponse{Success: false, Message: "Method not allowed"})
		return
	}
	var req SetGroupBoolRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.ChatJID == "" {
		writeJSON(http.StatusBadRequest, GenericResponse{Success: false, Message: "chat_jid is required"})
		return
	}
	jid, err := types.ParseJID(req.ChatJID)
	if err != nil {
		writeJSON(http.StatusBadRequest, GenericResponse{Success: false, Message: err.Error()})
		return
	}
	if err := apply(jid, req.Value); err != nil {
		writeJSON(http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
		return
	}
	writeJSON(http.StatusOK, GenericResponse{Success: true, Message: fmt.Sprintf("Group %s updated", label)})
}
