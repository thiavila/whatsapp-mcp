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