import base64
import email.utils
import logging
import os
import re

import bs4
import compressed_rtf
import RTFDE

from . import constants
from .attachment import Attachment, BrokenAttachment, UnsupportedAttachment
from .exceptions import UnrecognizedMSGTypeError
from .msg import MSGFile
from .recipient import Recipient
from .utils import addNumToDir, inputToBytes, inputToString, prepareFilename
from email.parser import Parser as EmailParser
from imapclient.imapclient import decode_utf7

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class MessageBase(MSGFile):
    """
    Base class for Message like msg files.
    """
    def __init__(self, path, prefix = '', attachmentClass = Attachment, filename = None,
                 delayAttachments = False, overrideEncoding = None,
                 attachmentErrorBehavior = constants.ATTACHMENT_ERROR_THROW, recipientSeparator = ';'):
        """
        :param path: path to the msg file in the system or is the raw msg file.
        :param prefix: used for extracting embeded msg files
            inside the main one. Do not set manually unless
            you know what you are doing.
        :param attachmentClass: optional, the class the Message object
            will use for attachments. You probably should
            not change this value unless you know what you
            are doing.
        :param filename: optional, the filename to be used by default when
            saving.
        :param delayAttachments: optional, delays the initialization of
            attachments until the user attempts to retrieve them. Allows MSG
            files with bad attachments to be initialized so the other data can
            be retrieved.
        :param overrideEncoding: optional, an encoding to use instead of the one
            specified by the msg file. Do not report encoding errors caused by
            this.
        :param attachmentErrorBehavior: Optional, the behaviour to use in the
            event of an error when parsing the attachments.
        :param recipientSeparator: Optional, Separator string to use between
            recipients.
        """
        super().__init__(path, prefix, attachmentClass, filename, overrideEncoding, attachmentErrorBehavior)
        self.__attachmentsDelayed = delayAttachments
        self.__attachmentsReady = False
        self.__recipientSeparator = recipientSeparator
        # Initialize properties in the order that is least likely to cause bugs.
        # TODO have each function check for initialization of needed data so these
        # lines will be unnecessary.
        self.mainProperties
        self.header
        self.recipients
        if not delayAttachments:
            self.attachments
        self.to
        self.cc
        self.sender
        self.date
        self.__crlf = '\n'  # This variable keeps track of what the new line character should be
        self.body
        self.named

    def _genRecipient(self, recipientType, recipientInt):
        """
        Returns the specified recipient field.
        """
        private = '_' + recipientType
        try:
            return getattr(self, private)
        except AttributeError:
            value = None
            # Check header first.
            if self.headerInit():
                value = self.header[recipientType]
                if value:
                    value = value.replace(',', self.__recipientSeparator)

            # If the header had a blank field or didn't have the field, generate it manually.
            if not value:
                # Check if the header has initialized.
                if self.headerInit():
                    logger.info(f'Header found, but "{recipientType}" is not included. Will be generated from other streams.')

                # Get a list of the recipients of the specified type.
                foundRecipients = tuple(recipient.formatted for recipient in self.recipients if recipient.type & 0x0000000f == recipientInt)

                # If we found recipients, join them with the recipient separator and a space.
                if len(foundRecipients) > 0:
                    value = (self.__recipientSeparator + ' ').join(foundRecipients)

            # Code to fix the formatting so it's all a single line. This allows the user to format it themself if they want.
            # This should probably be redone to use re or something, but I can do that later. This shouldn't be a huge problem for now.
            if value:
                value = value.replace(' \r\n\t', ' ').replace('\r\n\t ', ' ').replace('\r\n\t', ' ')
                value = value.replace('\r\n', ' ').replace('\r', ' ').replace('\n', ' ')
                while value.find('  ') != -1:
                    value = value.replace('  ', ' ')

            # Set the field in the class.
            setattr(self, private, value)

            return value

    def _registerNamedProperty(self, entry, _type, name = None):
        if self.attachmentsDelayed and not self.attachmentsReady:
            try:
                self.__waitingProperties
            except AttributeError:
                self.__waitingProperties = []
            self.__waitingProperties.append((entry, _type, name))
        else:
            for attachment in self.attachments:
                attachment._registerNamedProperty(entry, _type, name)

        super()._registerNamedProperty(entry, _type, name)

    def close(self) -> None:
        try:
            # If this throws an AttributeError then we have not loaded the attachments.
            self._attachments
            for attachment in self.attachments:
                if attachment.type == 'msg':
                    attachment.data.close()
        except AttributeError:
            pass
        super().close()

    def headerInit(self) -> bool:
        """
        Checks whether the header has been initialized.
        """
        try:
            self._header
            return True
        except AttributeError:
            return False

    def saveAttachments(self, **kwargs) -> None:
        """
        Saves only attachments in the same folder.
        """
        for attachment in self.attachments:
            attachment.save(**kwargs)

    @property
    def attachments(self):
        """
        Returns a list of all attachments.
        """
        try:
            return self._attachments
        except AttributeError:
            # Get the attachments
            attachmentDirs = []
            prefixLen = self.prefixLen
            for dir_ in self.listDir(False, True):
                if dir_[prefixLen].startswith('__attach') and\
                        dir_[prefixLen] not in attachmentDirs:
                    attachmentDirs.append(dir_[prefixLen])

            self._attachments = []

            for attachmentDir in attachmentDirs:
                try:
                    self._attachments.append(self.attachmentClass(self, attachmentDir))
                except (NotImplementedError, UnrecognizedMSGTypeError) as e:
                    if self.attachmentErrorBehavior > constants.ATTACHMENT_ERROR_THROW:
                        logger.error(f'Error processing attachment at {attachmentDir}')
                        logger.exception(e)
                        self._attachments.append(UnsupportedAttachment(self, attachmentDir))
                    else:
                        raise
                except Exception as e:
                    if self.attachmentErrorBehavior == constants.ATTACHMENT_ERROR_BROKEN:
                        logger.error(f'Error processing attachment at {attachmentDir}')
                        logger.exception(e)
                        self._attachments.append(BrokenAttachment(self, attachmentDir))
                    else:
                        raise

            self.__attachmentsReady = True
            try:
                self.__waitingProperties
                if self.__attachmentsDelayed:
                    for attachment in self._attachments:
                        for prop in self.__waitingProperties:
                            attachment._registerNamedProperty(*prop)
            except:
                pass

            return self._attachments

    @property
    def attachmentsDelayed(self):
        """
        Returns True if the attachment initialization was delayed.
        """
        return self.__attachmentsDelayed

    @property
    def attachmentsReady(self):
        """
        Returns True if the attachments are ready to be used.
        """
        return self.__attachmentsReady

    @property
    def bcc(self):
        """
        Returns the bcc field, if it exists.
        """
        return self._genRecipient('bcc', 3)

    @property
    def body(self):
        """
        Returns the message body, if it exists.
        """
        try:
            return self._body
        except AttributeError:
            self._body = self._getStringStream('__substg1.0_1000')
            if self._body:
                self._body = inputToString(self._body, 'utf-8')
                a = re.search('\n', self._body)
                if a is not None:
                    if re.search('\r\n', self._body) is not None:
                        self.__crlf = '\r\n'
            else:
                # If the body doesn't exist, see if we can get it from the RTF
                # body.
                if self.deencapsulatedRtf and self.deencapsulatedRtf.content_type == 'text':
                    self._body = self.deencapsulatedRtf.text
            return self._body

    @property
    def cc(self):
        """
        Returns the cc field, if it exists.
        """
        return self._genRecipient('cc', 2)

    @property
    def compressedRtf(self):
        """
        Returns the compressed RTF stream, if it exists.
        """
        return self._ensureSet('_compressedRtf', '__substg1.0_10090102', False)

    @property
    def crlf(self):
        """
        Returns the value of self.__crlf, should you need it for whatever
        reason.
        """
        self.body
        return self.__crlf

    @property
    def date(self):
        """
        Returns the send date, if it exists.
        """
        try:
            return self._date
        except AttributeError:
            self._date = self._prop.date
            return self._date

    @property
    def deencapsulatedRtf(self) -> RTFDE.DeEncapsulator:
        """
        Returns the instance of the deencapsulated RTF body.
        """
        try:
            return self._deencapsultor
        except AttributeError:
            if self.rtfBody:
                # If there is an RTF body, we try to deencapsulate it.
                try:
                    self._deencapsultor = RTFDE.DeEncapsulator(self.rtfBody)
                    self._deencapsultor.deencapsulate()
                except RTFDE.exceptions.NotEncapsulatedRtf as e:
                    logger.debug("RTF body is not encapsulated.")
                    self._deencapsultor = None
                except RTFDE.exceptions.MalformedEncapsulatedRtf as _e:
                    logger.info("RTF body contains malformed encapsulated content.")
                    self._deencapsultor = None
            else:
                self._deencapsultor = None
            return self._deencapsultor

    @property
    def defaultFolderName(self) -> str:
        """
        Generates the default name of the save folder.
        """
        try:
            return self._defaultFolderName
        except AttributeError:
            d = self.parsedDate

            dirName = '{0:02d}-{1:02d}-{2:02d}_{3:02d}{4:02d}'.format(*d) if d else 'UnknownDate'
            dirName += ' ' + (prepareFilename(self.subject) if self.subject else '[No subject]')

            self._defaultFolderName = dirName
            return dirName

    @property
    def header(self):
        """
        Returns the message header, if it exists. Otherwise it will generate
        one.
        """
        try:
            return self._header
        except AttributeError:
            headerText = self._getStringStream('__substg1.0_007D')
            if headerText:
                self._header = EmailParser().parsestr(headerText)
                self._header['date'] = self.date
            else:
                logger.info('Header is empty or was not found. Header will be generated from other streams.')
                header = EmailParser().parsestr('')
                header.add_header('Date', self.date)
                header.add_header('From', self.sender)
                header.add_header('To', self.to)
                header.add_header('Cc', self.cc)
                header.add_header('Bcc', self.bcc)
                header.add_header('Message-Id', self.messageId)
                # TODO find authentication results outside of header
                header.add_header('Authentication-Results', None)
                self._header = header
            return self._header

    @property
    def headerDict(self) -> dict:
        """
        Returns a dictionary of the entries in the header
        """
        try:
            return self._headerDict
        except AttributeError:
            self._headerDict = dict(self.header._headers)
            try:
                self._headerDict.pop('Received')
            except KeyError:
                pass
            return self._headerDict

    @property
    def htmlBody(self) -> bytes:
        """
        Returns the html body, if it exists.
        """
        try:
            return self._htmlBody
        except AttributeError:
            if self._ensureSet('_htmlBody', '__substg1.0_10130102', False):
                # Reducing line repetition.
                pass
            elif self.rtfBody:
                logger.info('HTML body was not found, attempting to generate from RTF.')
                if self.deencapsulatedRtf and self.deencapsulatedRtf.content_type == 'html':
                    self._htmlBody = self.deencapsulatedRtf.html.encode('utf-8')
                else:
                    logger.info('Could not deencapsulate HTML from RTF body.')
            elif self.body:
                # Convert the plain text body to html.
                logger.info('HTML body was not found, attempting to generate from plain text body.')
                correctedBody = self.body.encode('utf-8').replace('\r', '').replace('\n', '</br>')
                self._htmlBody = f'<html><body>{correctedBody}</body></head>'
            else:
                logger.into('HTML body could not be found nor generated.')

            return self._htmlBody

    @property
    def htmlBodyPrepared(self) -> bytes:
        """
        Returns the HTML body that has (where possible) the embedded attachments
        inserted into the body.
        """
        # If we can't get an HTML body then we have nothing to do.
        if not self.htmlBody:
            return self.htmlBody

        # Create the BeautifulSoup instance to use.
        soup = bs4.BeautifulSoup(self.htmlBody, 'html.parser')

        # Get a list of image tags to see if we can inject into. If the source
        # of an image starts with "cid:" that means it is one of the attachments
        # and is using the content id of that attachment.
        tags = (tag for tag in soup.findAll('img') if tag.get('src') and tag.get('src').startswith('cid:'))

        for tag in tags:
            # Iterate through the attachments until we get the right one.
            cid = tag['src'][4:]
            data = next((attachment.data for attachment in self.attachments if attachment.cid == cid), None)
            # If we found anything, inject it.
            if data:
                tag['src'] = (b'data:image;base64,' + base64.b64encode(data)).decode('utf-8')

        return soup.prettify('utf-8')

    @property
    def inReplyTo(self) -> str:
        """
        Returns the message id that this message is in reply to.
        """
        return self._ensureSet('_in_reply_to', '__substg1.0_1042')

    @property
    def isRead(self) -> bool:
        """
        Returns if this email has been marked as read.
        """
        return bool(self.mainProperties['0E070003'].value & 1)

    @property
    def messageId(self):
        try:
            return self._messageId
        except AttributeError:
            headerResult = None
            if self.headerInit():
                headerResult = self._header['message-id']
            if headerResult is not None:
                self._messageId = headerResult
            else:
                if self.headerInit():
                    logger.info('Header found, but "Message-Id" is not included. Will be generated from other streams.')
                self._messageId = self._getStringStream('__substg1.0_1035')
            return self._messageId

    @property
    def parsedDate(self):
        return email.utils.parsedate(self.date)

    @property
    def recipientSeparator(self) -> str:
        return self.__recipientSeparator

    @property
    def recipients(self) -> list:
        """
        Returns a list of all recipients.
        """
        try:
            return self._recipients
        except AttributeError:
            # Get the recipients
            recipientDirs = []
            prefixLen = self.prefixLen
            for dir_ in self.listDir():
                if dir_[prefixLen].startswith('__recip') and\
                        dir_[prefixLen] not in recipientDirs:
                    recipientDirs.append(dir_[prefixLen])

            self._recipients = []

            for recipientDir in recipientDirs:
                self._recipients.append(Recipient(recipientDir, self))

            return self._recipients

    @property
    def rtfBody(self) -> bytes:
        """
        Returns the decompressed Rtf body from the message.
        """
        try:
            return self._rtfBody
        except AttributeError:
            self._rtfBody = compressed_rtf.decompress(self.compressedRtf) if self.compressedRtf else None
            return self._rtfBody

    @property
    def sender(self) -> str:
        """
        Returns the message sender, if it exists.
        """
        try:
            return self._sender
        except AttributeError:
            # Check header first
            if self.headerInit():
                headerResult = self.header['from']
                if headerResult is not None:
                    self._sender = headerResult
                    return headerResult
                logger.info('Header found, but "sender" is not included. Will be generated from other streams.')
            # Extract from other fields
            text = self._getStringStream('__substg1.0_0C1A')
            email = self._getStringStream('__substg1.0_5D01')
            # Will not give an email address sometimes. Seems to exclude the email address if YOU are the sender.
            result = None
            if text is None:
                result = email
            else:
                result = text
                if email is not None:
                    result += ' <' + email + '>'

            self._sender = result
            return result

    @property
    def subject(self):
        """
        Returns the message subject, if it exists.
        """
        return self._ensureSet('_subject', '__substg1.0_0037')

    @property
    def to(self):
        """
        Returns the to field, if it exists.
        """
        return self._genRecipient('to', 1)
