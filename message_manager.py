from typing import Optional, Union, Callable, Awaitable, Any, List, Dict
import logging
from aiogram import Bot, types
from aiogram.types import Message, CallbackQuery
from database import User
from templates import MessageTemplates

logger = logging.getLogger(__name__)


class MessageManager:
    """
    Enhanced message manager that handles both template processing and message sending
    """

    def __init__(self, bot: Bot):
        self.bot = bot

    async def send_template(
            self,
            user: User,
            template_key: Union[str, List[str]],
            update: Union[Message, CallbackQuery],
            variables: Optional[Dict[str, Any]] = None,
            edit: bool = False,
            delete_original: bool = False,
            override_media_id: Optional[str] = None,
            media_type: Optional[str] = None
    ) -> None:
        """
        Main interface for sending template-based messages.
        """
        try:
            # Detecting need of closing callback
            needs_callback_answer = (
                    isinstance(update, CallbackQuery) and
                    (delete_original or not edit)
            )

            # Get template data
            template_data = await MessageTemplates.generate_screen(
                user=user,
                state_keys=template_key,
                variables=variables
            )

            if override_media_id:
                text, _, keyboard, parse_mode, disable_preview = template_data
                template_data = (text, override_media_id, keyboard, parse_mode, disable_preview)

            # Get chat_id and message_id
            if isinstance(update, CallbackQuery):
                chat_id = update.message.chat.id
                message_id = update.message.message_id
            else:
                chat_id = update.chat.id
                message_id = getattr(update, 'message_id', None)

            text, media_id, keyboard, parse_mode, disable_preview = template_data

            # Validate media_id to ensure it's a string
            valid_media = False
            if media_id:
                try:
                    # If media_id is an integer or any other type, try to convert it to string
                    if not isinstance(media_id, str):
                        media_id = str(media_id)
                    valid_media = True
                except (ValueError, TypeError):
                    logger.warning(f"Invalid media_id format: {media_id}, sending message without media")
                    media_id = None
                    valid_media = False

            # Handle message sending based on media presence and type
            if valid_media and media_id:
                if delete_original:
                    try:
                        await self.bot.delete_message(chat_id=chat_id, message_id=message_id)
                    except Exception as e:
                        logger.warning(f"Error deleting message: {e}")

                if media_type == 'video':
                    if edit and not delete_original:
                        try:
                            await self.bot.edit_message_media(
                                chat_id=chat_id,
                                message_id=message_id,
                                media=types.InputMediaVideo(
                                    media=media_id,
                                    caption=text,
                                    parse_mode=parse_mode
                                ),
                                reply_markup=keyboard
                            )
                        except Exception as e:
                            logger.error(f"Error editing message with video: {e}")
                            # Fallback to text-only message
                            await self.bot.edit_message_text(
                                chat_id=chat_id,
                                message_id=message_id,
                                text=text,
                                parse_mode=parse_mode,
                                reply_markup=keyboard,
                                disable_web_page_preview=disable_preview
                            )
                    else:
                        try:
                            await self.bot.send_video(
                                chat_id=chat_id,
                                video=media_id,
                                caption=text,
                                parse_mode=parse_mode,
                                reply_markup=keyboard
                            )
                        except Exception as e:
                            logger.error(f"Error sending video: {e}")
                            # Fallback to text-only message
                            await self.bot.send_message(
                                chat_id=chat_id,
                                text=text,
                                parse_mode=parse_mode,
                                reply_markup=keyboard,
                                disable_web_page_preview=disable_preview
                            )
                else:  # Default to photo
                    if edit and not delete_original:
                        try:
                            await self.bot.edit_message_media(
                                chat_id=chat_id,
                                message_id=message_id,
                                media=types.InputMediaPhoto(
                                    media=media_id,
                                    caption=text,
                                    parse_mode=parse_mode
                                ),
                                reply_markup=keyboard
                            )
                        except Exception as e:
                            logger.error(f"Error editing message with photo: {e}")
                            # Fallback to text-only message
                            await self.bot.edit_message_text(
                                chat_id=chat_id,
                                message_id=message_id,
                                text=text,
                                parse_mode=parse_mode,
                                reply_markup=keyboard,
                                disable_web_page_preview=disable_preview
                            )
                    else:
                        try:
                            await self.bot.send_photo(
                                chat_id=chat_id,
                                photo=media_id,
                                caption=text,
                                parse_mode=parse_mode,
                                reply_markup=keyboard
                            )
                        except Exception as e:
                            logger.error(f"Error sending photo: {e}")
                            # Fallback to text-only message
                            await self.bot.send_message(
                                chat_id=chat_id,
                                text=text,
                                parse_mode=parse_mode,
                                reply_markup=keyboard,
                                disable_web_page_preview=disable_preview
                            )
            else:
                # Handle text-only messages
                if delete_original:
                    try:
                        await self.bot.delete_message(chat_id=chat_id, message_id=message_id)
                    except Exception as e:
                        logger.warning(f"Error deleting message: {e}")

                kwargs = {
                    'chat_id': chat_id,
                    'text': text,
                    'parse_mode': parse_mode,
                    'reply_markup': keyboard,
                    'disable_web_page_preview': disable_preview
                }

                if edit and not delete_original:
                    kwargs['message_id'] = message_id
                    await self.bot.edit_message_text(**kwargs)
                else:
                    await self.bot.send_message(**kwargs)

            # Закрываем callback если нужно
            if needs_callback_answer:
                try:
                    await update.answer()
                except Exception as e:
                    logger.warning(f"Error answering callback query: {e}")

        except Exception as e:
            logger.error(f"Error sending template message: {e}")
            if isinstance(update, CallbackQuery):
                await update.answer("Error processing message")
            else:
                try:
                    await update.answer("Error processing message")
                except Exception as reply_error:
                    logger.error(f"Failed to send error message: {reply_error}")

    async def _create_send_function(
            self,
            template_data: tuple,
            update: Union[Message, CallbackQuery],
            edit: bool = False,
            delete_original: bool = False,
            media_type: str = 'photo'
    ) -> Callable[[], Awaitable[Any]]:
        """
        Internal method to create appropriate message sending function.
        Similar to the previous handle_message but as a private implementation detail.
        """
        try:
            text, media_id, keyboard, parse_mode, disable_preview = template_data

            # Get chat_id and message_id
            if isinstance(update, CallbackQuery):
                chat_id = update.message.chat.id
                message_id = update.message.message_id
            else:
                chat_id = update.chat.id
                message_id = update.message_id

            # Base kwargs for all message types
            kwargs = {
                'reply_markup': keyboard,
                'parse_mode': parse_mode
            }

            # Validate media_id
            valid_media = False
            if media_id:
                try:
                    # If media_id is an integer or any other type, try to convert it to string
                    if not isinstance(media_id, str):
                        media_id = str(media_id)
                    valid_media = True
                except (ValueError, TypeError):
                    logger.warning(f"Invalid media_id format: {media_id}, sending message without media")
                    media_id = None
                    valid_media = False

            # Handle media messages
            if valid_media and media_id:
                if media_type == 'video':
                    if delete_original:
                        await self.bot.delete_message(chat_id=chat_id, message_id=message_id)
                        return lambda: self.bot.send_video(
                            chat_id=chat_id,
                            video=media_id,
                            caption=text,
                            **kwargs
                        )
                    elif edit:
                        return lambda: self.bot.edit_message_media(
                            chat_id=chat_id,
                            message_id=message_id,
                            media=types.InputMediaVideo(
                                media=media_id,
                                caption=text,
                                parse_mode=parse_mode
                            ),
                            reply_markup=keyboard
                        )
                else:  # Photo by default
                    if edit:
                        return lambda: self.bot.edit_message_media(
                            chat_id=chat_id,
                            message_id=message_id,
                            media=types.InputMediaPhoto(
                                media=media_id,
                                caption=text,
                                parse_mode=parse_mode
                            ),
                            reply_markup=keyboard
                        )
                    else:
                        return lambda: self.bot.send_photo(
                            chat_id=chat_id,
                            photo=media_id,
                            caption=text,
                            **kwargs
                        )

            # Handle text messages
            else:
                kwargs['disable_web_page_preview'] = disable_preview
                if edit:
                    return lambda: self.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=text,
                        **kwargs
                    )
                else:
                    return lambda: self.bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        **kwargs
                    )

        except Exception as e:
            logger.error(f"Error creating send function: {e}")
            return lambda: update.answer("Error processing message")