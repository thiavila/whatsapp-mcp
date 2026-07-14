import sqlite3
import re
from datetime import datetime
from dataclasses import dataclass
from typing import Optional, List, Tuple, Dict, Any
import os.path
import requests
import json
import audio
import random
import time

MESSAGES_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'whatsapp-bridge', 'store', 'messages.db')
WHATSAPP_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'whatsapp-bridge', 'store', 'whatsapp.db')
WHATSAPP_API_BASE_URL = "http://localhost:8080/api"

TYPING_CHARS_PER_SECOND = 12.0
TYPING_JITTER_MIN = 0.75
TYPING_JITTER_MAX = 1.25
TYPING_DELAY_MIN_SECONDS = 1.0
TYPING_DELAY_MAX_SECONDS = 12.0

PN_SERVER = "s.whatsapp.net"
LID_SERVER = "lid"

@dataclass
class Message:
    timestamp: datetime
    sender: str
    content: str
    is_from_me: bool
    chat_jid: str
    id: str
    chat_name: Optional[str] = None
    media_type: Optional[str] = None

@dataclass
class Chat:
    jid: str
    name: Optional[str]
    last_message_time: Optional[datetime]
    last_message: Optional[str] = None
    last_sender: Optional[str] = None
    last_is_from_me: Optional[bool] = None

    @property
    def is_group(self) -> bool:
        """Determine if chat is a group based on JID pattern."""
        return self.jid.endswith("@g.us")

@dataclass
class Contact:
    phone_number: str
    name: Optional[str]
    jid: str

@dataclass
class MessageContext:
    message: Message
    before: List[Message]
    after: List[Message]


@dataclass(frozen=True)
class ChatIdentity:
    """All WhatsApp identifiers that refer to one direct conversation."""

    canonical_jid: str
    phone_number: Optional[str]
    phone_jid: Optional[str]
    lid_jid: Optional[str]
    aliases: Tuple[str, ...]

    @property
    def sender_ids(self) -> Tuple[str, ...]:
        return tuple(alias.split("@", 1)[0] for alias in self.aliases)


def _jid_parts(identifier: str) -> Tuple[str, Optional[str]]:
    value = (identifier or "").strip()
    if "@" in value:
        user, server = value.split("@", 1)
        return user, server
    return re.sub(r"\D", "", value), None


def resolve_chat_identity(identifier: str) -> ChatIdentity:
    """Resolve a phone number, PN JID, or LID JID to one stable identity.

    WhatsApp may send a direct message under a privacy-preserving LID even when
    the outgoing message used the contact's phone-number JID. The mapping is
    maintained by whatsmeow in ``whatsmeow_lid_map``.
    """
    user, server = _jid_parts(identifier)
    if not user:
        return ChatIdentity(identifier, None, None, None, (identifier,))

    # Groups, newsletters, broadcasts, and other non-direct JIDs have no PN/LID alias.
    if server not in (None, PN_SERVER, LID_SERVER):
        jid = identifier if "@" in identifier else user
        return ChatIdentity(jid, None, None, None, (jid,))

    phone_number = user if server in (None, PN_SERVER) else None
    lid = user if server == LID_SERVER else None

    try:
        conn = sqlite3.connect(f"file:{WHATSAPP_DB_PATH}?mode=ro", uri=True)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT lid, pn FROM whatsmeow_lid_map WHERE lid = ? OR pn = ? LIMIT 1",
            (user, user),
        )
        mapping = cursor.fetchone()
        if mapping:
            lid, phone_number = mapping
    except sqlite3.Error:
        # Alias resolution is best-effort so the MCP remains usable before the
        # whatsmeow store is initialized or while an old database is migrating.
        pass
    finally:
        if 'conn' in locals():
            conn.close()

    phone_jid = f"{phone_number}@{PN_SERVER}" if phone_number else None
    lid_jid = f"{lid}@{LID_SERVER}" if lid else None
    aliases = tuple(dict.fromkeys(jid for jid in (phone_jid, lid_jid) if jid))
    if not aliases:
        fallback = identifier if "@" in identifier else f"{user}@{PN_SERVER}"
        aliases = (fallback,)

    return ChatIdentity(
        canonical_jid=phone_jid or lid_jid or aliases[0],
        phone_number=phone_number,
        phone_jid=phone_jid,
        lid_jid=lid_jid,
        aliases=aliases,
    )


def _in_clause(values: Tuple[str, ...]) -> str:
    return ", ".join("?" for _ in values)


def _get_whatsmeow_contact_name(identity: ChatIdentity) -> Optional[str]:
    """Return the best contact/business name stored by whatsmeow."""
    try:
        conn = sqlite3.connect(f"file:{WHATSAPP_DB_PATH}?mode=ro", uri=True)
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT COALESCE(
                NULLIF(business_name, ''),
                NULLIF(full_name, ''),
                NULLIF(push_name, ''),
                NULLIF(first_name, '')
            )
            FROM whatsmeow_contacts
            WHERE their_jid IN ({_in_clause(identity.aliases)})
            ORDER BY
                CASE WHEN business_name IS NOT NULL AND business_name != '' THEN 0 ELSE 1 END,
                their_jid
            LIMIT 1
        """, identity.aliases)
        result = cursor.fetchone()
        return result[0] if result and result[0] else None
    except sqlite3.Error:
        return None
    finally:
        if 'conn' in locals():
            conn.close()

def get_sender_name(sender_jid: str) -> str:
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        identity = resolve_chat_identity(sender_jid)
        contact_name = _get_whatsmeow_contact_name(identity)
        if contact_name:
            return contact_name

        aliases = identity.aliases
        cursor.execute(f"""
            SELECT name
            FROM chats
            WHERE jid IN ({_in_clause(aliases)})
            ORDER BY last_message_time DESC
            LIMIT 1
        """, aliases)
        
        result = cursor.fetchone()
        
        # If no result, try looking for the number within JIDs
        if not result:
            # Extract the phone number part if it's a JID
            if '@' in sender_jid:
                phone_part = sender_jid.split('@')[0]
            else:
                phone_part = sender_jid
                
            cursor.execute("""
                SELECT name
                FROM chats
                WHERE jid LIKE ?
                LIMIT 1
            """, (f"%{phone_part}%",))
            
            result = cursor.fetchone()
        
        if result and result[0]:
            return result[0]
        else:
            return sender_jid
        
    except sqlite3.Error as e:
        print(f"Database error while getting sender name: {e}")
        return sender_jid
    finally:
        if 'conn' in locals():
            conn.close()

def format_message(message: Message, show_chat_info: bool = True) -> None:
    """Print a single message with consistent formatting."""
    output = ""
    
    if show_chat_info and message.chat_name:
        output += f"[{message.timestamp:%Y-%m-%d %H:%M:%S}] Chat: {message.chat_name} "
    else:
        output += f"[{message.timestamp:%Y-%m-%d %H:%M:%S}] "
        
    content_prefix = ""
    if hasattr(message, 'media_type') and message.media_type:
        content_prefix = f"[{message.media_type} - Message ID: {message.id} - Chat JID: {message.chat_jid}] "
    
    try:
        sender_name = get_sender_name(message.sender) if not message.is_from_me else "Me"
        output += f"From: {sender_name}: {content_prefix}{message.content}\n"
    except Exception as e:
        print(f"Error formatting message: {e}")
    return output

def format_messages_list(messages: List[Message], show_chat_info: bool = True) -> None:
    output = ""
    if not messages:
        output += "No messages to display."
        return output
    
    for message in messages:
        output += format_message(message, show_chat_info)
    return output

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
) -> List[Message]:
    """Get messages matching the specified criteria with optional context."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        # Build base query
        query_parts = ["SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.media_type FROM messages"]
        query_parts.append("JOIN chats ON messages.chat_jid = chats.jid")
        where_clauses = []
        params = []
        
        # Add filters
        if after:
            try:
                after = datetime.fromisoformat(after)
            except ValueError:
                raise ValueError(f"Invalid date format for 'after': {after}. Please use ISO-8601 format.")
            
            where_clauses.append("messages.timestamp > ?")
            params.append(after)

        if before:
            try:
                before = datetime.fromisoformat(before)
            except ValueError:
                raise ValueError(f"Invalid date format for 'before': {before}. Please use ISO-8601 format.")
            
            where_clauses.append("messages.timestamp < ?")
            params.append(before)

        if sender_phone_number:
            sender_ids = resolve_chat_identity(sender_phone_number).sender_ids
            where_clauses.append(f"messages.sender IN ({_in_clause(sender_ids)})")
            params.extend(sender_ids)
            
        if chat_jid:
            chat_aliases = resolve_chat_identity(chat_jid).aliases
            where_clauses.append(f"messages.chat_jid IN ({_in_clause(chat_aliases)})")
            params.extend(chat_aliases)
            
        if query:
            where_clauses.append("LOWER(messages.content) LIKE LOWER(?)")
            params.append(f"%{query}%")
            
        if where_clauses:
            query_parts.append("WHERE " + " AND ".join(where_clauses))
            
        # Add pagination
        offset = page * limit
        query_parts.append("ORDER BY messages.timestamp DESC")
        query_parts.append("LIMIT ? OFFSET ?")
        params.extend([limit, offset])
        
        cursor.execute(" ".join(query_parts), tuple(params))
        messages = cursor.fetchall()
        
        result = []
        for msg in messages:
            message = Message(
                timestamp=datetime.fromisoformat(msg[0]),
                sender=msg[1],
                chat_name=msg[2],
                content=msg[3],
                is_from_me=msg[4],
                chat_jid=msg[5],
                id=msg[6],
                media_type=msg[7]
            )
            result.append(message)
            
        if include_context and result:
            # Add context for each message
            messages_with_context = []
            for msg in result:
                context = get_message_context(
                    msg.id,
                    context_before,
                    context_after,
                    chat_jid=msg.chat_jid,
                )
                messages_with_context.extend(context.before)
                messages_with_context.append(context.message)
                messages_with_context.extend(context.after)
            
            return format_messages_list(messages_with_context, show_chat_info=True)
            
        # Format and display messages without context
        return format_messages_list(result, show_chat_info=True)    
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def get_message_context(
    message_id: str,
    before: int = 5,
    after: int = 5,
    chat_jid: Optional[str] = None,
) -> MessageContext:
    """Get context around a specific message."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        # Get the target message first
        target_where = ["messages.id = ?"]
        target_params: List[Any] = [message_id]
        if chat_jid:
            target_aliases = resolve_chat_identity(chat_jid).aliases
            target_where.append(f"messages.chat_jid IN ({_in_clause(target_aliases)})")
            target_params.extend(target_aliases)

        cursor.execute(f"""
            SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.chat_jid, messages.media_type
            FROM messages
            JOIN chats ON messages.chat_jid = chats.jid
            WHERE {" AND ".join(target_where)}
            ORDER BY messages.timestamp DESC
            LIMIT 1
        """, tuple(target_params))
        msg_data = cursor.fetchone()
        
        if not msg_data:
            raise ValueError(f"Message with ID {message_id} not found")
            
        target_message = Message(
            timestamp=datetime.fromisoformat(msg_data[0]),
            sender=msg_data[1],
            chat_name=msg_data[2],
            content=msg_data[3],
            is_from_me=msg_data[4],
            chat_jid=msg_data[5],
            id=msg_data[6],
            media_type=msg_data[8]
        )
        
        # Get messages before
        context_aliases = resolve_chat_identity(msg_data[7]).aliases
        context_placeholders = _in_clause(context_aliases)

        cursor.execute(f"""
            SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.media_type
            FROM messages
            JOIN chats ON messages.chat_jid = chats.jid
            WHERE messages.chat_jid IN ({context_placeholders}) AND messages.timestamp < ?
            ORDER BY messages.timestamp DESC
            LIMIT ?
        """, (*context_aliases, msg_data[0], before))
        
        before_messages = []
        for msg in cursor.fetchall():
            before_messages.append(Message(
                timestamp=datetime.fromisoformat(msg[0]),
                sender=msg[1],
                chat_name=msg[2],
                content=msg[3],
                is_from_me=msg[4],
                chat_jid=msg[5],
                id=msg[6],
                media_type=msg[7]
            ))
        
        # Get messages after
        cursor.execute(f"""
            SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.media_type
            FROM messages
            JOIN chats ON messages.chat_jid = chats.jid
            WHERE messages.chat_jid IN ({context_placeholders}) AND messages.timestamp > ?
            ORDER BY messages.timestamp ASC
            LIMIT ?
        """, (*context_aliases, msg_data[0], after))
        
        after_messages = []
        for msg in cursor.fetchall():
            after_messages.append(Message(
                timestamp=datetime.fromisoformat(msg[0]),
                sender=msg[1],
                chat_name=msg[2],
                content=msg[3],
                is_from_me=msg[4],
                chat_jid=msg[5],
                id=msg[6],
                media_type=msg[7]
            ))
        
        return MessageContext(
            message=target_message,
            before=before_messages,
            after=after_messages
        )
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        raise
    finally:
        if 'conn' in locals():
            conn.close()


def list_chats(
    query: Optional[str] = None,
    limit: int = 20,
    page: int = 0,
    include_last_message: bool = True,
    sort_by: str = "last_active"
) -> List[Chat]:
    """Get chats matching the specified criteria."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        # Build base query
        query_parts = ["""
            SELECT 
                chats.jid,
                chats.name,
                chats.last_message_time,
                messages.content as last_message,
                messages.sender as last_sender,
                messages.is_from_me as last_is_from_me
            FROM chats
        """]
        
        if include_last_message:
            query_parts.append("""
                LEFT JOIN messages ON chats.jid = messages.chat_jid 
                AND chats.last_message_time = messages.timestamp
            """)
            
        where_clauses = []
        params = []
        
        if query:
            aliases = resolve_chat_identity(query).aliases
            where_clauses.append(
                f"(LOWER(chats.name) LIKE LOWER(?) OR chats.jid LIKE ? "
                f"OR chats.jid IN ({_in_clause(aliases)}))"
            )
            params.extend([f"%{query}%", f"%{query}%", *aliases])
            
        if where_clauses:
            query_parts.append("WHERE " + " AND ".join(where_clauses))
            
        # Add sorting
        order_by = "chats.last_message_time DESC" if sort_by == "last_active" else "chats.name"
        query_parts.append(f"ORDER BY {order_by}")
        
        # Add pagination
        offset = (page ) * limit
        query_parts.append("LIMIT ? OFFSET ?")
        params.extend([limit, offset])
        
        cursor.execute(" ".join(query_parts), tuple(params))
        chats = cursor.fetchall()
        
        result = []
        for chat_data in chats:
            chat = Chat(
                jid=chat_data[0],
                name=chat_data[1],
                last_message_time=datetime.fromisoformat(chat_data[2]) if chat_data[2] else None,
                last_message=chat_data[3],
                last_sender=chat_data[4],
                last_is_from_me=chat_data[5]
            )
            result.append(chat)
            
        return result
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def search_contacts(query: str) -> List[Contact]:
    """Search contacts by name or phone number."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        # Split query into characters to support partial matching
        search_pattern = '%' +query + '%'
        
        identity = resolve_chat_identity(query)
        cursor.execute(f"""
            SELECT DISTINCT 
                jid,
                name,
                last_message_time
            FROM chats
            WHERE 
                ((LOWER(name) LIKE LOWER(?) OR LOWER(jid) LIKE LOWER(?))
                OR jid IN ({_in_clause(identity.aliases)}))
                AND jid NOT LIKE '%@g.us'
            ORDER BY last_message_time DESC, name, jid
            LIMIT 50
        """, (search_pattern, search_pattern, *identity.aliases))
        
        contacts = cursor.fetchall()
        
        result = []
        seen = set()
        for contact_data in contacts:
            resolved = resolve_chat_identity(contact_data[0])
            if resolved.canonical_jid in seen:
                continue
            seen.add(resolved.canonical_jid)
            contact = Contact(
                phone_number=resolved.phone_number or contact_data[0].split('@')[0],
                name=contact_data[1],
                jid=resolved.canonical_jid,
            )
            result.append(contact)
            
        return result
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def get_contact_chats(jid: str, limit: int = 20, page: int = 0) -> List[Chat]:
    """Get all chats involving the contact.
    
    Args:
        jid: The contact's JID to search for
        limit: Maximum number of chats to return (default 20)
        page: Page number for pagination (default 0)
    """
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        identity = resolve_chat_identity(jid)
        sender_ids = identity.sender_ids
        cursor.execute(f"""
            SELECT DISTINCT
                c.jid,
                c.name,
                c.last_message_time,
                m.content as last_message,
                m.sender as last_sender,
                m.is_from_me as last_is_from_me
            FROM chats c
            JOIN messages m ON c.jid = m.chat_jid
            WHERE m.sender IN ({_in_clause(sender_ids)})
               OR c.jid IN ({_in_clause(identity.aliases)})
            ORDER BY c.last_message_time DESC
            LIMIT ? OFFSET ?
        """, (*sender_ids, *identity.aliases, limit, page * limit))
        
        chats = cursor.fetchall()
        
        result = []
        for chat_data in chats:
            chat = Chat(
                jid=chat_data[0],
                name=chat_data[1],
                last_message_time=datetime.fromisoformat(chat_data[2]) if chat_data[2] else None,
                last_message=chat_data[3],
                last_sender=chat_data[4],
                last_is_from_me=chat_data[5]
            )
            result.append(chat)
            
        return result
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def get_last_interaction(jid: str) -> str:
    """Get most recent message involving the contact."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        identity = resolve_chat_identity(jid)
        sender_ids = identity.sender_ids
        cursor.execute(f"""
            SELECT 
                m.timestamp,
                m.sender,
                c.name,
                m.content,
                m.is_from_me,
                c.jid,
                m.id,
                m.media_type
            FROM messages m
            JOIN chats c ON m.chat_jid = c.jid
            WHERE m.sender IN ({_in_clause(sender_ids)})
               OR c.jid IN ({_in_clause(identity.aliases)})
            ORDER BY m.timestamp DESC
            LIMIT 1
        """, (*sender_ids, *identity.aliases))
        
        msg_data = cursor.fetchone()
        
        if not msg_data:
            return None
            
        message = Message(
            timestamp=datetime.fromisoformat(msg_data[0]),
            sender=msg_data[1],
            chat_name=msg_data[2],
            content=msg_data[3],
            is_from_me=msg_data[4],
            chat_jid=msg_data[5],
            id=msg_data[6],
            media_type=msg_data[7]
        )
        
        return format_message(message)
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None
    finally:
        if 'conn' in locals():
            conn.close()


def get_chat(chat_jid: str, include_last_message: bool = True) -> Optional[Chat]:
    """Get chat metadata by JID."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        query = """
            SELECT 
                c.jid,
                c.name,
                c.last_message_time,
                m.content as last_message,
                m.sender as last_sender,
                m.is_from_me as last_is_from_me
            FROM chats c
        """
        
        if include_last_message:
            query += """
                LEFT JOIN messages m ON c.jid = m.chat_jid 
                AND c.last_message_time = m.timestamp
            """
            
        aliases = resolve_chat_identity(chat_jid).aliases
        query += f" WHERE c.jid IN ({_in_clause(aliases)}) ORDER BY c.last_message_time DESC LIMIT 1"
        
        cursor.execute(query, aliases)
        chat_data = cursor.fetchone()
        
        if not chat_data:
            return None
            
        return Chat(
            jid=chat_data[0],
            name=chat_data[1],
            last_message_time=datetime.fromisoformat(chat_data[2]) if chat_data[2] else None,
            last_message=chat_data[3],
            last_sender=chat_data[4],
            last_is_from_me=chat_data[5]
        )
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None
    finally:
        if 'conn' in locals():
            conn.close()


def get_direct_chat_by_contact(sender_phone_number: str) -> Optional[Chat]:
    """Get chat metadata by sender phone number."""
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        
        aliases = resolve_chat_identity(sender_phone_number).aliases
        cursor.execute(f"""
            SELECT 
                c.jid,
                c.name,
                c.last_message_time,
                m.content as last_message,
                m.sender as last_sender,
                m.is_from_me as last_is_from_me
            FROM chats c
            LEFT JOIN messages m ON c.jid = m.chat_jid 
                AND c.last_message_time = m.timestamp
            WHERE c.jid IN ({_in_clause(aliases)}) AND c.jid NOT LIKE '%@g.us'
            ORDER BY c.last_message_time DESC
            LIMIT 1
        """, aliases)
        
        chat_data = cursor.fetchone()
        
        if not chat_data:
            return None
            
        return Chat(
            jid=chat_data[0],
            name=chat_data[1],
            last_message_time=datetime.fromisoformat(chat_data[2]) if chat_data[2] else None,
            last_message=chat_data[3],
            last_sender=chat_data[4],
            last_is_from_me=chat_data[5]
        )
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None
    finally:
        if 'conn' in locals():
            conn.close()

def _typing_delay_seconds(message: str) -> float:
    """Return a bounded, length-based typing delay with natural jitter."""
    base_delay = len(message) / TYPING_CHARS_PER_SECOND
    jittered_delay = base_delay * random.uniform(TYPING_JITTER_MIN, TYPING_JITTER_MAX)
    return min(
        TYPING_DELAY_MAX_SECONDS,
        max(TYPING_DELAY_MIN_SECONDS, jittered_delay),
    )


def send_message(
    recipient: str, message: str, show_typing: bool = True
) -> Tuple[bool, str]:
    typing_jid = None
    typing_started = False
    try:
        # Validate input
        if not recipient:
            return False, "Recipient must be provided"

        if show_typing:
            typing_jid = resolve_chat_identity(recipient).canonical_jid
            typing_started, _ = send_typing_indicator(typing_jid, is_typing=True)
            if typing_started:
                time.sleep(_typing_delay_seconds(message))
        
        url = f"{WHATSAPP_API_BASE_URL}/send"
        payload = {
            "recipient": recipient,
            "message": message,
        }
        
        response = requests.post(url, json=payload)
        
        # Check if the request was successful
        if response.status_code == 200:
            result = response.json()
            return result.get("success", False), result.get("message", "Unknown response")
        else:
            return False, f"Error: HTTP {response.status_code} - {response.text}"
            
    except requests.RequestException as e:
        return False, f"Request error: {str(e)}"
    except json.JSONDecodeError:
        return False, f"Error parsing response: {response.text}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"
    finally:
        if typing_started and typing_jid:
            # Clearing presence is best-effort and must not change send status.
            send_typing_indicator(typing_jid, is_typing=False)

def send_file(recipient: str, media_path: str) -> Tuple[bool, str]:
    try:
        # Validate input
        if not recipient:
            return False, "Recipient must be provided"
        
        if not media_path:
            return False, "Media path must be provided"
        
        if not os.path.isfile(media_path):
            return False, f"Media file not found: {media_path}"
        
        url = f"{WHATSAPP_API_BASE_URL}/send"
        payload = {
            "recipient": recipient,
            "media_path": media_path
        }
        
        response = requests.post(url, json=payload)
        
        # Check if the request was successful
        if response.status_code == 200:
            result = response.json()
            return result.get("success", False), result.get("message", "Unknown response")
        else:
            return False, f"Error: HTTP {response.status_code} - {response.text}"
            
    except requests.RequestException as e:
        return False, f"Request error: {str(e)}"
    except json.JSONDecodeError:
        return False, f"Error parsing response: {response.text}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"

def send_audio_message(recipient: str, media_path: str) -> Tuple[bool, str]:
    try:
        # Validate input
        if not recipient:
            return False, "Recipient must be provided"
        
        if not media_path:
            return False, "Media path must be provided"
        
        if not os.path.isfile(media_path):
            return False, f"Media file not found: {media_path}"

        if not media_path.endswith(".ogg"):
            try:
                media_path = audio.convert_to_opus_ogg_temp(media_path)
            except Exception as e:
                return False, f"Error converting file to opus ogg. You likely need to install ffmpeg: {str(e)}"
        
        url = f"{WHATSAPP_API_BASE_URL}/send"
        payload = {
            "recipient": recipient,
            "media_path": media_path
        }
        
        response = requests.post(url, json=payload)
        
        # Check if the request was successful
        if response.status_code == 200:
            result = response.json()
            return result.get("success", False), result.get("message", "Unknown response")
        else:
            return False, f"Error: HTTP {response.status_code} - {response.text}"
            
    except requests.RequestException as e:
        return False, f"Request error: {str(e)}"
    except json.JSONDecodeError:
        return False, f"Error parsing response: {response.text}"
    except Exception as e:
        return False, f"Unexpected error: {str(e)}"

def download_media(message_id: str, chat_jid: str) -> Optional[str]:
    """Download media from a message and return the local file path.
    
    Args:
        message_id: The ID of the message containing the media
        chat_jid: The JID of the chat containing the message
    
    Returns:
        The local file path if download was successful, None otherwise
    """
    try:
        url = f"{WHATSAPP_API_BASE_URL}/download"
        payload = {
            "message_id": message_id,
            "chat_jid": chat_jid
        }
        
        response = requests.post(url, json=payload)
        
        if response.status_code == 200:
            result = response.json()
            if result.get("success", False):
                path = result.get("path")
                print(f"Media downloaded successfully: {path}")
                return path
            else:
                print(f"Download failed: {result.get('message', 'Unknown error')}")
                return None
        else:
            print(f"Error: HTTP {response.status_code} - {response.text}")
            return None
            
    except requests.RequestException as e:
        print(f"Request error: {str(e)}")
        return None
    except json.JSONDecodeError:
        print(f"Error parsing response: {response.text}")
        return None
    except Exception as e:
        print(f"Unexpected error: {str(e)}")
        return None


def _get_json(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """GET a JSON response from the bridge with consistent error handling."""
    try:
        response = requests.get(f"{WHATSAPP_API_BASE_URL}{path}", params=params)
        if response.status_code != 200:
            return {"success": False, "message": f"HTTP {response.status_code}: {response.text}"}
        return response.json()
    except (requests.RequestException, json.JSONDecodeError) as e:
        return {"success": False, "message": str(e)}


def _post_json(path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """POST and return the full parsed JSON body."""
    try:
        response = requests.post(f"{WHATSAPP_API_BASE_URL}{path}", json=payload)
        return response.json()
    except (requests.RequestException, json.JSONDecodeError) as e:
        return {"success": False, "message": str(e)}


def _post_simple(path: str, payload: Dict[str, Any]) -> Tuple[bool, str]:
    """POST a JSON body to the bridge and unpack a {success, message} response.

    Used by the dozen-ish action endpoints that all return the same shape.
    Centralises HTTP/JSON exception handling.
    """
    try:
        response = requests.post(f"{WHATSAPP_API_BASE_URL}{path}", json=payload)
        result = response.json()
        return bool(result.get("success", False)), result.get("message", "Unknown response")
    except requests.RequestException as e:
        return False, f"Request error: {str(e)}"
    except json.JSONDecodeError:
        return False, f"Error parsing response: {response.text}"


# --- Rich messaging ---

def edit_message(chat_jid: str, message_id: str, new_content: str) -> Tuple[bool, str]:
    """Edit a message you previously sent (within WhatsApp's 20-minute window)."""
    return _post_simple(
        "/messages/edit",
        {"chat_jid": chat_jid, "message_id": message_id, "new_content": new_content},
    )


def delete_message(chat_jid: str, message_id: str) -> Tuple[bool, str]:
    """Revoke (delete-for-everyone) a message you sent."""
    return _post_simple(
        "/messages/delete",
        {"chat_jid": chat_jid, "message_id": message_id},
    )


def react_to_message(
    chat_jid: str,
    message_id: str,
    emoji: str,
    sender_jid: str = "",
    is_from_me: bool = False,
) -> Tuple[bool, str]:
    """React (or clear a reaction with emoji='') to a message.

    For incoming DMs, leave ``sender_jid`` empty. For groups, pass the
    participant JID. For your own messages, set ``is_from_me=True``.
    """
    return _post_simple(
        "/messages/react",
        {
            "chat_jid": chat_jid,
            "message_id": message_id,
            "sender_jid": sender_jid,
            "is_from_me": is_from_me,
            "emoji": emoji,
        },
    )


def mark_messages_read(chat_jid: str, message_ids: List[str], sender_jid: str = "") -> Tuple[bool, str]:
    """Send read receipts for specific messages."""
    return _post_simple(
        "/messages/mark-read",
        {"chat_jid": chat_jid, "message_ids": message_ids, "sender_jid": sender_jid},
    )


def send_typing_indicator(
    chat_jid: str, is_typing: bool = True, is_recording_audio: bool = False
) -> Tuple[bool, str]:
    """Send "composing" / "recording audio" / "paused" presence to a chat."""
    return _post_simple(
        "/messages/typing",
        {
            "chat_jid": chat_jid,
            "is_typing": is_typing,
            "is_recording_audio": is_recording_audio,
        },
    )


def create_poll(
    chat_jid: str,
    name: str,
    options: List[str],
    selectable_option_count: int = 1,
) -> Tuple[bool, str]:
    """Send a poll message. ``selectable_option_count``=1 → single choice."""
    return _post_simple(
        "/messages/poll",
        {
            "chat_jid": chat_jid,
            "name": name,
            "options": options,
            "selectable_option_count": selectable_option_count,
        },
    )


def check_phones_on_whatsapp(phones: List[str]) -> List[Dict[str, Any]]:
    """Check whether each phone number is registered on WhatsApp.

    Phone numbers must be in E.164 format with the leading ``+``.
    """
    try:
        response = requests.post(f"{WHATSAPP_API_BASE_URL}/contacts/check", json={"phones": phones})
        if response.status_code != 200:
            return []
        return response.json().get("results", []) or []
    except (requests.RequestException, json.JSONDecodeError):
        return []


def set_disappearing_timer(chat_jid: str, timer: str) -> Tuple[bool, str]:
    """Set disappearing messages timer for a chat. Accepts: off, 24h, 7d, 90d."""
    return _post_simple(
        "/chats/disappearing-timer",
        {"chat_jid": chat_jid, "timer": timer},
    )


# --- Groups ---

def create_group(name: str, participants: List[str]) -> Dict[str, Any]:
    """Create a new WhatsApp group."""
    return _post_json("/groups/create", {"name": name, "participants": participants})


def leave_group(chat_jid: str) -> Tuple[bool, str]:
    return _post_simple("/groups/leave", {"chat_jid": chat_jid})


def get_group_info(chat_jid: str) -> Dict[str, Any]:
    return _get_json("/groups/info", {"chat_jid": chat_jid})


def list_joined_groups() -> Dict[str, Any]:
    return _get_json("/groups/list")


def get_group_invite_link(chat_jid: str, reset: bool = False) -> Dict[str, Any]:
    return _post_json("/groups/invite-link", {"chat_jid": chat_jid, "reset": reset})


def get_group_info_from_link(link: str) -> Dict[str, Any]:
    return _post_json("/groups/info-from-link", {"link": link})


def join_group_with_link(link: str) -> Dict[str, Any]:
    return _post_json("/groups/join", {"link": link})


def update_group_participants(chat_jid: str, participants: List[str], action: str) -> Tuple[bool, str]:
    return _post_simple(
        "/groups/participants",
        {"chat_jid": chat_jid, "participants": participants, "action": action},
    )


def set_group_name(chat_jid: str, name: str) -> Tuple[bool, str]:
    return _post_simple("/groups/name", {"chat_jid": chat_jid, "name": name})


def set_group_description(chat_jid: str, description: str) -> Tuple[bool, str]:
    return _post_simple("/groups/description", {"chat_jid": chat_jid, "description": description})


def set_group_photo(chat_jid: str, photo_path: str) -> Dict[str, Any]:
    return _post_json("/groups/photo", {"chat_jid": chat_jid, "photo_path": photo_path})


def set_group_announce(chat_jid: str, announce: bool) -> Tuple[bool, str]:
    return _post_simple("/groups/announce", {"chat_jid": chat_jid, "value": announce})


def set_group_locked(chat_jid: str, locked: bool) -> Tuple[bool, str]:
    return _post_simple("/groups/locked", {"chat_jid": chat_jid, "value": locked})


def set_group_join_approval_mode(chat_jid: str, required: bool) -> Tuple[bool, str]:
    return _post_simple("/groups/approval-mode", {"chat_jid": chat_jid, "value": required})


def get_group_join_requests(chat_jid: str) -> List[Dict[str, str]]:
    res = _get_json("/groups/requests", {"chat_jid": chat_jid})
    return res.get("requests", []) or []


def decide_group_join_requests(chat_jid: str, participants: List[str], approve: bool) -> Tuple[bool, str]:
    return _post_simple(
        "/groups/requests/decide",
        {"chat_jid": chat_jid, "participants": participants, "approve": approve},
    )


# --- Contacts / users ---

def get_user_info(jids: List[str]) -> List[Dict[str, Any]]:
    res = _post_json("/users/info", {"jids": jids})
    return res.get("users", []) or []


def get_profile_picture(jid: str, preview: bool = False) -> Dict[str, Any]:
    return _post_json("/users/profile-picture", {"jid": jid, "preview": preview})


def get_business_profile(jid: str) -> Dict[str, Any]:
    return _post_json("/users/business-profile", {"jid": jid})


def get_blocklist() -> List[str]:
    res = _get_json("/users/blocklist")
    return res.get("jids", []) or []


def block_contact(jid: str, block: bool) -> Tuple[bool, str]:
    return _post_simple("/users/block", {"jid": jid, "block": block})


def set_status_message(message: str) -> Tuple[bool, str]:
    return _post_simple("/users/status-message", {"message": message})


def set_privacy_setting(setting_type: str, value: str) -> Tuple[bool, str]:
    return _post_simple(
        "/users/privacy",
        {"setting_type": setting_type, "value": value},
    )


def resolve_business_link(link: str) -> Dict[str, Any]:
    return _post_json("/users/resolve-business-link", {"link": link})


# --- Labels ---

def list_labels(include_deleted: bool = False) -> List[Dict[str, Any]]:
    """Fetch known WhatsApp Business labels from the bridge's local store.

    The store is populated from app-state sync events, so newly created labels
    only appear here after the bridge has received the corresponding sync.
    """
    try:
        url = f"{WHATSAPP_API_BASE_URL}/labels"
        params = {"include_deleted": "true"} if include_deleted else None
        response = requests.get(url, params=params)
        if response.status_code != 200:
            return []
        return response.json().get("labels", []) or []
    except (requests.RequestException, json.JSONDecodeError):
        return []


def get_chats_with_label(label_id: str) -> List[str]:
    """Return chat JIDs that currently carry the given label."""
    try:
        url = f"{WHATSAPP_API_BASE_URL}/labels/chats"
        response = requests.get(url, params={"label_id": label_id})
        if response.status_code != 200:
            return []
        return response.json().get("chats", []) or []
    except (requests.RequestException, json.JSONDecodeError):
        return []


def get_messages_with_label(label_id: str) -> List[Dict[str, str]]:
    """Return {chat_jid, message_id} pairs that currently carry the given label."""
    try:
        url = f"{WHATSAPP_API_BASE_URL}/labels/messages"
        response = requests.get(url, params={"label_id": label_id})
        if response.status_code != 200:
            return []
        return response.json().get("messages", []) or []
    except (requests.RequestException, json.JSONDecodeError):
        return []


def upsert_label(label_id: str, name: str, color: int, deleted: bool) -> Tuple[bool, str, Optional[str]]:
    """Create / edit / delete a label (unified endpoint).

    Pass an empty ``label_id`` to create a new one — the bridge generates the ID
    and returns it. Pass ``deleted=True`` to tombstone an existing label.
    """
    try:
        url = f"{WHATSAPP_API_BASE_URL}/labels/edit"
        payload = {
            "label_id": label_id,
            "name": name,
            "color": color,
            "deleted": deleted,
        }
        response = requests.post(url, json=payload)
        result = response.json()
        return (
            bool(result.get("success", False)),
            result.get("message", "Unknown response"),
            result.get("label_id"),
        )
    except requests.RequestException as e:
        return False, f"Request error: {str(e)}", None
    except json.JSONDecodeError:
        return False, f"Error parsing response: {response.text}", None


def label_chat(label_id: str, chat_jid: str, labeled: bool) -> Tuple[bool, str]:
    """Add (``labeled=True``) or remove (``labeled=False``) a label from a chat."""
    try:
        url = f"{WHATSAPP_API_BASE_URL}/labels/chat"
        payload = {"label_id": label_id, "chat_jid": chat_jid, "labeled": labeled}
        response = requests.post(url, json=payload)
        result = response.json()
        return bool(result.get("success", False)), result.get("message", "Unknown response")
    except requests.RequestException as e:
        return False, f"Request error: {str(e)}"
    except json.JSONDecodeError:
        return False, f"Error parsing response: {response.text}"


def label_message(label_id: str, chat_jid: str, message_id: str, labeled: bool) -> Tuple[bool, str]:
    """Add or remove a label from a specific message."""
    try:
        url = f"{WHATSAPP_API_BASE_URL}/labels/message"
        payload = {
            "label_id": label_id,
            "chat_jid": chat_jid,
            "message_id": message_id,
            "labeled": labeled,
        }
        response = requests.post(url, json=payload)
        result = response.json()
        return bool(result.get("success", False)), result.get("message", "Unknown response")
    except requests.RequestException as e:
        return False, f"Request error: {str(e)}"
    except json.JSONDecodeError:
        return False, f"Error parsing response: {response.text}"


def get_unread_messages(limit: int = 10) -> List[Dict[str, Any]]:
    """Get chats with unread messages, most recent first.

    Returns chats where unread_count != 0 — that catches both real positive
    counts (synced from the phone) and the -1 sentinel set by MarkChatAsUnread
    on the bridge side. Includes the last message preview so the caller can
    decide whether each is worth opening.
    """
    try:
        conn = sqlite3.connect(MESSAGES_DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT
                chats.jid,
                chats.name,
                chats.last_message_time,
                chats.unread_count,
                messages.content AS last_message,
                messages.sender  AS last_sender,
                messages.is_from_me AS last_is_from_me
            FROM chats
            LEFT JOIN messages
              ON chats.jid = messages.chat_jid
             AND chats.last_message_time = messages.timestamp
            WHERE chats.unread_count != 0
            ORDER BY chats.last_message_time DESC
            LIMIT ?
        """, (limit,))

        result: List[Dict[str, Any]] = []
        for row in cursor.fetchall():
            jid, name, last_time, unread_count, last_msg, last_sender, last_is_from_me = row
            result.append({
                "jid": jid,
                "name": name,
                "last_message_time": last_time,
                "unread_count": unread_count,
                "unread_count_unknown": unread_count == -1,
                "last_message": last_msg,
                "last_sender": last_sender,
                "last_is_from_me": bool(last_is_from_me) if last_is_from_me is not None else None,
            })
        return result

    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return []
    finally:
        if 'conn' in locals():
            conn.close()
