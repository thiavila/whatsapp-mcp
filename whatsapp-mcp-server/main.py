from typing import List, Dict, Any, Optional
from mcp.server.fastmcp import FastMCP
from whatsapp import (
    search_contacts as whatsapp_search_contacts,
    list_messages as whatsapp_list_messages,
    list_chats as whatsapp_list_chats,
    get_chat as whatsapp_get_chat,
    get_direct_chat_by_contact as whatsapp_get_direct_chat_by_contact,
    get_contact_chats as whatsapp_get_contact_chats,
    get_last_interaction as whatsapp_get_last_interaction,
    get_message_context as whatsapp_get_message_context,
    send_message as whatsapp_send_message,
    send_file as whatsapp_send_file,
    send_audio_message as whatsapp_audio_voice_message,
    download_media as whatsapp_download_media,
    get_unread_messages as whatsapp_get_unread_messages,
    list_labels as whatsapp_list_labels,
    get_chats_with_label as whatsapp_get_chats_with_label,
    get_messages_with_label as whatsapp_get_messages_with_label,
    upsert_label as whatsapp_upsert_label,
    label_chat as whatsapp_label_chat,
    label_message as whatsapp_label_message,
    edit_message as whatsapp_edit_message,
    delete_message as whatsapp_delete_message,
    react_to_message as whatsapp_react_to_message,
    mark_messages_read as whatsapp_mark_messages_read,
    send_typing_indicator as whatsapp_send_typing_indicator,
    create_poll as whatsapp_create_poll,
    check_phones_on_whatsapp as whatsapp_check_phones_on_whatsapp,
    set_disappearing_timer as whatsapp_set_disappearing_timer,
    create_group as whatsapp_create_group,
    leave_group as whatsapp_leave_group,
    get_group_info as whatsapp_get_group_info,
    list_joined_groups as whatsapp_list_joined_groups,
    get_group_invite_link as whatsapp_get_group_invite_link,
    get_group_info_from_link as whatsapp_get_group_info_from_link,
    join_group_with_link as whatsapp_join_group_with_link,
    update_group_participants as whatsapp_update_group_participants,
    set_group_name as whatsapp_set_group_name,
    set_group_description as whatsapp_set_group_description,
    set_group_photo as whatsapp_set_group_photo,
    set_group_announce as whatsapp_set_group_announce,
    set_group_locked as whatsapp_set_group_locked,
    set_group_join_approval_mode as whatsapp_set_group_join_approval_mode,
    get_group_join_requests as whatsapp_get_group_join_requests,
    decide_group_join_requests as whatsapp_decide_group_join_requests,
    get_user_info as whatsapp_get_user_info,
    get_profile_picture as whatsapp_get_profile_picture,
    get_business_profile as whatsapp_get_business_profile,
    get_blocklist as whatsapp_get_blocklist,
    block_contact as whatsapp_block_contact,
    set_status_message as whatsapp_set_status_message,
    set_privacy_setting as whatsapp_set_privacy_setting,
    resolve_business_link as whatsapp_resolve_business_link,
)

# Initialize FastMCP server
mcp = FastMCP("whatsapp")

@mcp.tool()
def search_contacts(query: str) -> List[Dict[str, Any]]:
    """Search WhatsApp contacts by name or phone number.
    
    Args:
        query: Search term to match against contact names or phone numbers
    """
    contacts = whatsapp_search_contacts(query)
    return contacts

@mcp.tool()
def list_messages(
    after: Optional[str] = None,
    before: Optional[str] = None,
    sender_phone_number: Optional[str] = None,
    chat_jid: Optional[str] = None,
    query: Optional[str] = None,
    limit: int = 20,
    page: int = 0,
    include_context: bool = True,
    context_before: int = 1,
    context_after: int = 1
) -> List[Dict[str, Any]]:
    """Get WhatsApp messages matching specified criteria with optional context.
    
    Args:
        after: Optional ISO-8601 formatted string to only return messages after this date
        before: Optional ISO-8601 formatted string to only return messages before this date
        sender_phone_number: Optional phone number to filter messages by sender
        chat_jid: Optional chat JID to filter messages by chat
        query: Optional search term to filter messages by content
        limit: Maximum number of messages to return (default 20)
        page: Page number for pagination (default 0)
        include_context: Whether to include messages before and after matches (default True)
        context_before: Number of messages to include before each match (default 1)
        context_after: Number of messages to include after each match (default 1)
    """
    messages = whatsapp_list_messages(
        after=after,
        before=before,
        sender_phone_number=sender_phone_number,
        chat_jid=chat_jid,
        query=query,
        limit=limit,
        page=page,
        include_context=include_context,
        context_before=context_before,
        context_after=context_after
    )
    return messages

@mcp.tool()
def list_chats(
    query: Optional[str] = None,
    limit: int = 20,
    page: int = 0,
    include_last_message: bool = True,
    sort_by: str = "last_active"
) -> List[Dict[str, Any]]:
    """Get WhatsApp chats matching specified criteria.
    
    Args:
        query: Optional search term to filter chats by name or JID
        limit: Maximum number of chats to return (default 20)
        page: Page number for pagination (default 0)
        include_last_message: Whether to include the last message in each chat (default True)
        sort_by: Field to sort results by, either "last_active" or "name" (default "last_active")
    """
    chats = whatsapp_list_chats(
        query=query,
        limit=limit,
        page=page,
        include_last_message=include_last_message,
        sort_by=sort_by
    )
    return chats

@mcp.tool()
def get_chat(chat_jid: str, include_last_message: bool = True) -> Dict[str, Any]:
    """Get WhatsApp chat metadata by JID.
    
    Args:
        chat_jid: The JID of the chat to retrieve
        include_last_message: Whether to include the last message (default True)
    """
    chat = whatsapp_get_chat(chat_jid, include_last_message)
    return chat

@mcp.tool()
def get_direct_chat_by_contact(sender_phone_number: str) -> Dict[str, Any]:
    """Get WhatsApp chat metadata by sender phone number.
    
    Args:
        sender_phone_number: The phone number to search for
    """
    chat = whatsapp_get_direct_chat_by_contact(sender_phone_number)
    return chat

@mcp.tool()
def get_contact_chats(jid: str, limit: int = 20, page: int = 0) -> List[Dict[str, Any]]:
    """Get all WhatsApp chats involving the contact.
    
    Args:
        jid: The contact's JID to search for
        limit: Maximum number of chats to return (default 20)
        page: Page number for pagination (default 0)
    """
    chats = whatsapp_get_contact_chats(jid, limit, page)
    return chats

@mcp.tool()
def get_last_interaction(jid: str) -> str:
    """Get most recent WhatsApp message involving the contact.
    
    Args:
        jid: The JID of the contact to search for
    """
    message = whatsapp_get_last_interaction(jid)
    return message

@mcp.tool()
def get_message_context(
    message_id: str,
    before: int = 5,
    after: int = 5
) -> Dict[str, Any]:
    """Get context around a specific WhatsApp message.
    
    Args:
        message_id: The ID of the message to get context for
        before: Number of messages to include before the target message (default 5)
        after: Number of messages to include after the target message (default 5)
    """
    context = whatsapp_get_message_context(message_id, before, after)
    return context

@mcp.tool()
def send_message(
    recipient: str,
    message: str
) -> Dict[str, Any]:
    """Send a WhatsApp message to a person or group. For group chats use the JID.

    Args:
        recipient: The recipient - either a phone number with country code but no + or other symbols,
                 or a JID (e.g., "123456789@s.whatsapp.net" or a group JID like "123456789@g.us")
        message: The message text to send
    
    Returns:
        A dictionary containing success status and a status message
    """
    # Validate input
    if not recipient:
        return {
            "success": False,
            "message": "Recipient must be provided"
        }
    
    # Call the whatsapp_send_message function with the unified recipient parameter
    success, status_message = whatsapp_send_message(recipient, message)
    return {
        "success": success,
        "message": status_message
    }

@mcp.tool()
def send_file(recipient: str, media_path: str) -> Dict[str, Any]:
    """Send a file such as a picture, raw audio, video or document via WhatsApp to the specified recipient. For group messages use the JID.
    
    Args:
        recipient: The recipient - either a phone number with country code but no + or other symbols,
                 or a JID (e.g., "123456789@s.whatsapp.net" or a group JID like "123456789@g.us")
        media_path: The absolute path to the media file to send (image, video, document)
    
    Returns:
        A dictionary containing success status and a status message
    """
    
    # Call the whatsapp_send_file function
    success, status_message = whatsapp_send_file(recipient, media_path)
    return {
        "success": success,
        "message": status_message
    }

@mcp.tool()
def send_audio_message(recipient: str, media_path: str) -> Dict[str, Any]:
    """Send any audio file as a WhatsApp audio message to the specified recipient. For group messages use the JID. If it errors due to ffmpeg not being installed, use send_file instead.
    
    Args:
        recipient: The recipient - either a phone number with country code but no + or other symbols,
                 or a JID (e.g., "123456789@s.whatsapp.net" or a group JID like "123456789@g.us")
        media_path: The absolute path to the audio file to send (will be converted to Opus .ogg if it's not a .ogg file)
    
    Returns:
        A dictionary containing success status and a status message
    """
    success, status_message = whatsapp_audio_voice_message(recipient, media_path)
    return {
        "success": success,
        "message": status_message
    }

@mcp.tool()
def download_media(message_id: str, chat_jid: str) -> Dict[str, Any]:
    """Download media from a WhatsApp message and get the local file path.
    
    Args:
        message_id: The ID of the message containing the media
        chat_jid: The JID of the chat containing the message
    
    Returns:
        A dictionary containing success status, a status message, and the file path if successful
    """
    file_path = whatsapp_download_media(message_id, chat_jid)
    
    if file_path:
        return {
            "success": True,
            "message": "Media downloaded successfully",
            "file_path": file_path
        }
    else:
        return {
            "success": False,
            "message": "Failed to download media"
        }

@mcp.tool()
def edit_message(chat_jid: str, message_id: str, new_content: str) -> Dict[str, Any]:
    """Edit a message you previously sent. WhatsApp only allows edits within ~20 minutes.

    Args:
        chat_jid: The chat JID
        message_id: The message ID
        new_content: The new text
    """
    success, message = whatsapp_edit_message(chat_jid, message_id, new_content)
    return {"success": success, "message": message}


@mcp.tool()
def delete_message(chat_jid: str, message_id: str) -> Dict[str, Any]:
    """Delete (revoke for everyone) a message you sent.

    Args:
        chat_jid: The chat JID
        message_id: The message ID
    """
    success, message = whatsapp_delete_message(chat_jid, message_id)
    return {"success": success, "message": message}


@mcp.tool()
def react_to_message(
    chat_jid: str,
    message_id: str,
    emoji: str,
    sender_jid: str = "",
    is_from_me: bool = False,
) -> Dict[str, Any]:
    """React to a message with an emoji (or clear a reaction by passing emoji='').

    For incoming DMs, leave sender_jid empty. For incoming group messages, pass
    the participant JID. For your own messages, set is_from_me=True.

    Args:
        chat_jid: The chat JID
        message_id: The message ID
        emoji: The emoji to react with (e.g. "👍"). Empty string removes the reaction.
        sender_jid: Original sender JID — required for group messages
        is_from_me: Set True when reacting to your own messages
    """
    success, message = whatsapp_react_to_message(
        chat_jid, message_id, emoji, sender_jid=sender_jid, is_from_me=is_from_me
    )
    return {"success": success, "message": message}


@mcp.tool()
def mark_messages_read(
    chat_jid: str, message_ids: List[str], sender_jid: str = ""
) -> Dict[str, Any]:
    """Send read receipts for specific messages. Different from chat-level mark-read.

    Args:
        chat_jid: The chat JID
        message_ids: List of message IDs to mark as read
        sender_jid: Original sender JID; if omitted, derived from local store
    """
    success, message = whatsapp_mark_messages_read(chat_jid, message_ids, sender_jid=sender_jid)
    return {"success": success, "message": message}


@mcp.tool()
def send_typing_indicator(
    chat_jid: str, is_typing: bool = True, is_recording_audio: bool = False
) -> Dict[str, Any]:
    """Send a "composing..." or "recording audio..." indicator to a chat.

    Use is_typing=False to clear the indicator (sends "paused" presence).

    Args:
        chat_jid: The chat JID
        is_typing: True to send "composing", False to send "paused"
        is_recording_audio: When typing, set True to show "recording audio" instead
    """
    success, message = whatsapp_send_typing_indicator(chat_jid, is_typing, is_recording_audio)
    return {"success": success, "message": message}


@mcp.tool()
def create_poll(
    chat_jid: str,
    name: str,
    options: List[str],
    selectable_option_count: int = 1,
) -> Dict[str, Any]:
    """Send a poll message to a chat.

    Args:
        chat_jid: The chat JID
        name: The poll question
        options: List of option strings (minimum 2)
        selectable_option_count: 1 for single-choice, >1 for multi-choice
    """
    success, message = whatsapp_create_poll(chat_jid, name, options, selectable_option_count)
    return {"success": success, "message": message}


@mcp.tool()
def check_phones_on_whatsapp(phones: List[str]) -> List[Dict[str, Any]]:
    """Check which phone numbers are registered on WhatsApp.

    Args:
        phones: List of phone numbers in E.164 format WITH leading "+" (e.g. "+5511999998888")

    Returns:
        For each phone: query, jid, is_on_whatsapp, and verified_name if it's a Business account.
    """
    return whatsapp_check_phones_on_whatsapp(phones)


@mcp.tool()
def set_disappearing_timer(chat_jid: str, timer: str) -> Dict[str, Any]:
    """Set disappearing messages timer for a chat.

    Args:
        chat_jid: The chat JID
        timer: One of "off", "24h", "7d", "90d"
    """
    success, message = whatsapp_set_disappearing_timer(chat_jid, timer)
    return {"success": success, "message": message}


@mcp.tool()
def create_group(name: str, participants: List[str]) -> Dict[str, Any]:
    """Create a new WhatsApp group with the given participants.

    Args:
        name: Group display name (max ~25 chars)
        participants: List of phone numbers (with country code, no +) or JIDs

    Returns:
        Dict with success, message, group_jid (the new group's JID), and full info.
    """
    return whatsapp_create_group(name, participants)


@mcp.tool()
def leave_group(chat_jid: str) -> Dict[str, Any]:
    """Leave a group chat."""
    success, message = whatsapp_leave_group(chat_jid)
    return {"success": success, "message": message}


@mcp.tool()
def get_group_info(chat_jid: str) -> Dict[str, Any]:
    """Get full metadata for a group: participants, admins, settings, description.

    Args:
        chat_jid: The group JID (ends in @g.us)
    """
    return whatsapp_get_group_info(chat_jid)


@mcp.tool()
def list_joined_groups() -> Dict[str, Any]:
    """List all groups the linked WhatsApp account belongs to, with full metadata."""
    return whatsapp_list_joined_groups()


@mcp.tool()
def get_group_invite_link(chat_jid: str, reset: bool = False) -> Dict[str, Any]:
    """Get the invite link for a group (admins only).

    Args:
        chat_jid: The group JID
        reset: True to revoke the current link and generate a fresh one
    """
    return whatsapp_get_group_invite_link(chat_jid, reset)


@mcp.tool()
def get_group_info_from_link(link: str) -> Dict[str, Any]:
    """Look up a group's info from an invite link WITHOUT joining.

    Args:
        link: Full https://chat.whatsapp.com/<code> URL or just the code
    """
    return whatsapp_get_group_info_from_link(link)


@mcp.tool()
def join_group_with_link(link: str) -> Dict[str, Any]:
    """Join a group via an invite link.

    Args:
        link: Full https://chat.whatsapp.com/<code> URL or just the code
    """
    return whatsapp_join_group_with_link(link)


@mcp.tool()
def update_group_participants(chat_jid: str, participants: List[str], action: str) -> Dict[str, Any]:
    """Add, remove, promote, or demote participants in a group (admins only).

    Args:
        chat_jid: The group JID
        participants: List of phone numbers or JIDs to act on
        action: One of "add", "remove", "promote" (to admin), "demote" (from admin)
    """
    success, message = whatsapp_update_group_participants(chat_jid, participants, action)
    return {"success": success, "message": message}


@mcp.tool()
def set_group_name(chat_jid: str, name: str) -> Dict[str, Any]:
    """Rename a group (admin or anyone, depending on group lock setting)."""
    success, message = whatsapp_set_group_name(chat_jid, name)
    return {"success": success, "message": message}


@mcp.tool()
def set_group_description(chat_jid: str, description: str) -> Dict[str, Any]:
    """Update a group's description (the text below the group name in WhatsApp UI)."""
    success, message = whatsapp_set_group_description(chat_jid, description)
    return {"success": success, "message": message}


@mcp.tool()
def set_group_photo(chat_jid: str, photo_path: str) -> Dict[str, Any]:
    """Set or remove a group's photo.

    Args:
        chat_jid: The group JID
        photo_path: Absolute path to a JPEG file; pass empty string to remove the photo
    """
    return whatsapp_set_group_photo(chat_jid, photo_path)


@mcp.tool()
def set_group_announce_only(chat_jid: str, announce_only: bool) -> Dict[str, Any]:
    """Restrict messaging to admins (True) or allow everyone (False)."""
    success, message = whatsapp_set_group_announce(chat_jid, announce_only)
    return {"success": success, "message": message}


@mcp.tool()
def set_group_locked(chat_jid: str, locked: bool) -> Dict[str, Any]:
    """Restrict group info editing to admins (True) or allow everyone (False)."""
    success, message = whatsapp_set_group_locked(chat_jid, locked)
    return {"success": success, "message": message}


@mcp.tool()
def set_group_join_approval_required(chat_jid: str, required: bool) -> Dict[str, Any]:
    """Require admin approval before new members can join (True), or allow anyone with the link (False)."""
    success, message = whatsapp_set_group_join_approval_mode(chat_jid, required)
    return {"success": success, "message": message}


@mcp.tool()
def get_group_join_requests(chat_jid: str) -> List[Dict[str, str]]:
    """List pending join requests for a group with approval-required mode on."""
    return whatsapp_get_group_join_requests(chat_jid)


@mcp.tool()
def decide_group_join_requests(
    chat_jid: str, participants: List[str], approve: bool
) -> Dict[str, Any]:
    """Approve or reject pending join requests.

    Args:
        chat_jid: The group JID
        participants: List of JIDs of users to act on
        approve: True to approve, False to reject
    """
    success, message = whatsapp_decide_group_join_requests(chat_jid, participants, approve)
    return {"success": success, "message": message}


@mcp.tool()
def get_user_info(jids: List[str]) -> List[Dict[str, Any]]:
    """Get full user info: status message, profile picture ID, devices, verified business name.

    Args:
        jids: List of user JIDs (e.g. "5511999998888@s.whatsapp.net")
    """
    return whatsapp_get_user_info(jids)


@mcp.tool()
def get_profile_picture(jid: str, preview: bool = False) -> Dict[str, Any]:
    """Get a downloadable URL for a user or group profile picture.

    Args:
        jid: User or group JID
        preview: True for low-res thumbnail, False for full image
    """
    return whatsapp_get_profile_picture(jid, preview)


@mcp.tool()
def get_business_profile(jid: str) -> Dict[str, Any]:
    """Get business profile info (address, email, categories, hours) for a Business contact."""
    return whatsapp_get_business_profile(jid)


@mcp.tool()
def get_blocklist() -> List[str]:
    """List all currently blocked contact JIDs."""
    return whatsapp_get_blocklist()


@mcp.tool()
def block_contact(jid: str) -> Dict[str, Any]:
    """Block a contact (they can no longer message or call you)."""
    success, message = whatsapp_block_contact(jid, block=True)
    return {"success": success, "message": message}


@mcp.tool()
def unblock_contact(jid: str) -> Dict[str, Any]:
    """Unblock a previously blocked contact."""
    success, message = whatsapp_block_contact(jid, block=False)
    return {"success": success, "message": message}


@mcp.tool()
def set_status_message(message: str) -> Dict[str, Any]:
    """Update your own "About" / status message (the text shown under your name in your profile)."""
    success, message_text = whatsapp_set_status_message(message)
    return {"success": success, "message": message_text}


@mcp.tool()
def set_privacy_setting(setting_type: str, value: str) -> Dict[str, Any]:
    """Update a privacy setting.

    Args:
        setting_type: One of "groupadd", "last" (last seen), "status", "profile" (picture),
                      "readreceipts", "online", "calladd", "messages"
        value: Depends on setting_type. Common values: "all", "contacts", "contact_blacklist",
               "none", "match_last_seen" (for "online"), "known" (for "calladd").
    """
    success, msg = whatsapp_set_privacy_setting(setting_type, value)
    return {"success": success, "message": msg}


@mcp.tool()
def resolve_business_link(link: str) -> Dict[str, Any]:
    """Resolve a wa.me/p/<code> business link into JID, business name, and pre-filled greeting.

    Args:
        link: Full wa.me URL or just the code
    """
    return whatsapp_resolve_business_link(link)


@mcp.tool()
def list_labels(include_deleted: bool = False) -> List[Dict[str, Any]]:
    """List WhatsApp Business labels (the colored tags) known to the bridge.

    Labels are a WhatsApp Business feature — they will only exist if the linked
    account is a Business account. Returns ID, name, color, and deleted flag.

    Args:
        include_deleted: Include tombstoned labels (default False)
    """
    return whatsapp_list_labels(include_deleted)


@mcp.tool()
def get_chats_with_label(label_id: str) -> List[str]:
    """List chat JIDs currently tagged with the given label.

    Args:
        label_id: The label ID (get it via list_labels)
    """
    return whatsapp_get_chats_with_label(label_id)


@mcp.tool()
def get_messages_with_label(label_id: str) -> List[Dict[str, str]]:
    """List {chat_jid, message_id} pairs currently tagged with the given label.

    Args:
        label_id: The label ID (get it via list_labels)
    """
    return whatsapp_get_messages_with_label(label_id)


@mcp.tool()
def create_label(name: str, color: int = 0) -> Dict[str, Any]:
    """Create a new WhatsApp Business label.

    Args:
        name: Display name for the label
        color: Color index (0-19 in WhatsApp's palette; defaults to 0)

    Returns:
        Dict with success, message, and the generated label_id.
    """
    success, message, label_id = whatsapp_upsert_label(
        label_id="", name=name, color=color, deleted=False
    )
    return {"success": success, "message": message, "label_id": label_id}


@mcp.tool()
def edit_label(label_id: str, name: str, color: int = 0) -> Dict[str, Any]:
    """Edit an existing label's name or color.

    Args:
        label_id: ID of the label to edit
        name: New display name
        color: New color index
    """
    success, message, _ = whatsapp_upsert_label(
        label_id=label_id, name=name, color=color, deleted=False
    )
    return {"success": success, "message": message}


@mcp.tool()
def delete_label(label_id: str) -> Dict[str, Any]:
    """Delete (tombstone) a label across all linked devices.

    Args:
        label_id: ID of the label to delete
    """
    success, message, _ = whatsapp_upsert_label(
        label_id=label_id, name="", color=0, deleted=True
    )
    return {"success": success, "message": message}


@mcp.tool()
def add_label_to_chat(label_id: str, chat_jid: str) -> Dict[str, Any]:
    """Tag a chat with a label.

    Args:
        label_id: The label ID
        chat_jid: The chat JID (works for both individual chats and groups)
    """
    success, message = whatsapp_label_chat(label_id, chat_jid, labeled=True)
    return {"success": success, "message": message}


@mcp.tool()
def remove_label_from_chat(label_id: str, chat_jid: str) -> Dict[str, Any]:
    """Untag a chat (remove a label).

    Args:
        label_id: The label ID
        chat_jid: The chat JID
    """
    success, message = whatsapp_label_chat(label_id, chat_jid, labeled=False)
    return {"success": success, "message": message}


@mcp.tool()
def add_label_to_message(label_id: str, chat_jid: str, message_id: str) -> Dict[str, Any]:
    """Tag a specific message with a label.

    Args:
        label_id: The label ID
        chat_jid: The chat the message belongs to
        message_id: The message ID
    """
    success, message = whatsapp_label_message(label_id, chat_jid, message_id, labeled=True)
    return {"success": success, "message": message}


@mcp.tool()
def remove_label_from_message(label_id: str, chat_jid: str, message_id: str) -> Dict[str, Any]:
    """Untag a specific message.

    Args:
        label_id: The label ID
        chat_jid: The chat the message belongs to
        message_id: The message ID
    """
    success, message = whatsapp_label_message(label_id, chat_jid, message_id, labeled=False)
    return {"success": success, "message": message}


@mcp.tool()
def get_unread_messages(limit: int = 10) -> List[Dict[str, Any]]:
    """Get an overview of recent chats with unread messages.
    
    Args:
        limit: Maximum number of chats with unread messages to return (default 10)
    
    Returns:
        A list of chat objects with unread message information
    """
    unread_chats = whatsapp_get_unread_messages(limit)
    return unread_chats

if __name__ == "__main__":
    # Initialize and run the server
    mcp.run(transport='stdio')