# relay.py: PyLink Relay plugin
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pickle
import sched
import threading
import string
from collections import defaultdict

from expiringdict import ExpiringDict

import utils
from log import log
from conf import confname
import world

dbname = "pylinkrelay"
if confname != 'pylink':
    dbname += '-%s' % confname
dbname += '.db'

relayusers = defaultdict(dict)
relayservers = defaultdict(dict)
spawnlocks = defaultdict(threading.RLock)
savecache = ExpiringDict(max_len=5, max_age_seconds=10)
killcache = ExpiringDict(max_len=5, max_age_seconds=10)

def relayWhoisHandler(irc, target):
    user = irc.users[target]
    orig = getLocalUser(irc, target)
    if orig:
        network, remoteuid = orig
        remotenick = world.networkobjects[network].users[remoteuid].nick
        return [320, "%s :is a remote user connected via PyLink Relay. Home "
                     "network: %s; Home nick: %s" % (user.nick, network,
                                                     remotenick)]
world.whois_handlers.append(relayWhoisHandler)

def normalizeNick(irc, netname, nick, separator=None, uid=''):
    separator = separator or irc.serverdata.get('separator') or "/"
    log.debug('(%s) normalizeNick: using %r as separator.', irc.name, separator)
    orig_nick = nick
    protoname = irc.protoname
    maxnicklen = irc.maxnicklen
    if '/' not in separator or not protoname.startswith(('insp', 'unreal')):
        # Charybdis doesn't allow / in usernames, and will SQUIT with
        # a protocol violation if it sees one.
        separator = separator.replace('/', '|')
        nick = nick.replace('/', '|')
    if nick.startswith(tuple(string.digits)):
        # On TS6 IRCds, nicks that start with 0-9 are only allowed if
        # they match the UID of the originating server. Otherwise, you'll
        # get nasty protocol violation SQUITs!
        nick = '_' + nick
    tagnicks = True

    suffix = separator + netname
    nick = nick[:maxnicklen]
    # Maximum allowed length of a nickname, minus the obligatory /network tag.
    allowedlength = maxnicklen - len(suffix)

    # If a nick is too long, the real nick portion will be cut off, but the
    # /network suffix MUST remain the same.
    nick = nick[:allowedlength]
    nick += suffix

    # The nick we want exists? Darn, create another one then.
    # Increase the separator length by 1 if the user was already tagged,
    # but couldn't be created due to a nick conflict.
    # This can happen when someone steals a relay user's nick.

    # However, if the user is changing from, say, a long, cut-off nick to another long,
    # cut-off nick, we don't need to check for duplicates and tag the nick twice.

    # somecutoffnick/net would otherwise be erroneous NICK'ed to somecutoffnic//net,
    # even though there would be no collision because the old and new nicks are from
    # the same client.
    while utils.nickToUid(irc, nick) and utils.nickToUid(irc, nick) != uid:
        new_sep = separator + separator[-1]
        log.debug('(%s) normalizeNick: nick %r is in use; using %r as new_sep.', irc.name, nick, new_sep)
        nick = normalizeNick(irc, netname, orig_nick, separator=new_sep)
    finalLength = len(nick)
    assert finalLength <= maxnicklen, "Normalized nick %r went over max " \
        "nick length (got: %s, allowed: %s!)" % (nick, finalLength, maxnicklen)

    return nick

def loadDB():
    global db
    try:
        with open(dbname, "rb") as f:
            db = pickle.load(f)
    except (ValueError, IOError):
        log.exception("Relay: failed to load links database %s"
            ", creating a new one in memory...", dbname)
        db = {}

def exportDB(reschedule=False):
    scheduler = world.schedulers.get('relaydb')
    if reschedule and scheduler:
        scheduler.enter(30, 1, exportDB, argument=(True,))
    log.debug("Relay: exporting links database to %s", dbname)
    with open(dbname, 'wb') as f:
        pickle.dump(db, f, protocol=4)

@utils.add_cmd
def save(irc, source, args):
    """takes no arguments.

    Saves the relay database to disk."""
    if utils.isOper(irc, source):
        exportDB()
        irc.msg(source, 'Done.')
    else:
        irc.msg(source, 'Error: You are not authenticated!')
        return

def getPrefixModes(irc, remoteirc, channel, user):
    modes = ''
    for pmode in ('owner', 'admin', 'op', 'halfop', 'voice'):
        if pmode in remoteirc.cmodes:  # Mode supported by IRCd
            mlist = irc.channels[channel].prefixmodes[pmode+'s']
            log.debug('(%s) getPrefixModes: checking if %r is in %s list: %r',
                      irc.name, user, pmode, mlist)
            if user in mlist:
                modes += remoteirc.cmodes[pmode]
    return modes

def getRemoteSid(irc, remoteirc):
    """Get the remote server SID representing remoteirc on irc, spawning
    it if it doesn't exist."""
    with spawnlocks[irc.name]:
        try:
            sid = relayservers[irc.name][remoteirc.name]
        except KeyError:
            sid = irc.proto.spawnServer('%s.relay' % remoteirc.name)
            relayservers[irc.name][remoteirc.name] = sid
        return sid

def getRemoteUser(irc, remoteirc, user, spawnIfMissing=True):
    # If the user (stored here as {('netname', 'UID'):
    # {'network1': 'UID1', 'network2': 'UID2'}}) exists, don't spawn it
    # again!
    try:
        if user == irc.pseudoclient.uid:
            return remoteirc.pseudoclient.uid
    except AttributeError:  # Network hasn't been initialized yet?
        pass
    with spawnlocks[irc.name]:
        try:
            u = relayusers[(irc.name, user)][remoteirc.name]
        except KeyError:
            userobj = irc.users.get(user)
            if userobj is None or (not spawnIfMissing) or (not remoteirc.connected.is_set()):
                # The query wasn't actually a valid user, or the network hasn't
                # been connected yet... Oh well!
                return
            nick = normalizeNick(remoteirc, irc.name, userobj.nick)
            # Truncate idents at 10 characters, because TS6 won't like them otherwise!
            ident = userobj.ident[:10]
            # Ditto hostname at 64 chars.
            host = userobj.host[:64]
            realname = userobj.realname
            modes = getSupportedUmodes(irc, remoteirc, userobj.modes)
            opertype = ''
            if ('o', None) in userobj.modes:
                if hasattr(userobj, 'opertype'):
                    # InspIRCd's special OPERTYPE command; this is mandatory
                    # and setting of umode +/-o will fail unless this
                    # is used instead. This also sets an oper type for
                    # the user, which is used in WHOIS, etc.

                    # If an opertype exists for the user, add " (remote)"
                    # for the relayed clone, so that it shows in whois.
                    # Janus does this too. :)
                    # OPERTYPE uses underscores instead of spaces, FYI.
                    log.debug('(%s) relay.getRemoteUser: setting OPERTYPE of client for %r to %s',
                              irc.name, user, userobj.opertype)
                    opertype = userobj.opertype + '_(remote)'
                else:
                    opertype = 'IRC_Operator_(remote)'
                # Set hideoper on remote opers, to prevent inflating
                # /lusers and various /stats
                hideoper_mode = remoteirc.umodes.get('hideoper')
                if hideoper_mode:
                    modes.append((hideoper_mode, None))
            rsid = getRemoteSid(remoteirc, irc)
            u = remoteirc.proto.spawnClient(nick, ident=ident,
                                            host=host, realname=realname,
                                            modes=modes, ts=userobj.ts,
                                            opertype=opertype, server=rsid).uid
            remoteirc.users[u].remote = (irc.name, user)
            remoteirc.users[u].opertype = opertype
            away = userobj.away
            if away:
                remoteirc.proto.awayClient(u, away)
        relayusers[(irc.name, user)][remoteirc.name] = u
        return u

def handle_operup(irc, numeric, command, args):
    newtype = args['text'] + '_(remote)'
    for netname, user in relayusers[(irc.name, numeric)].items():
        log.debug('(%s) relay.handle_opertype: setting OPERTYPE of %s/%s to %s', irc.name, user, netname, newtype)
        remoteirc = world.networkobjects[netname]
        remoteirc.users[user].opertype = newtype
utils.add_hook(handle_operup, 'PYLINK_CLIENT_OPERED')

def getLocalUser(irc, user, targetirc=None):
    """<irc object> <pseudoclient uid> [<target irc object>]

    Returns a tuple with the home network name and the UID of the original
    user that <pseudoclient uid> was spawned for, where <pseudoclient uid>
    is the UID of a PyLink relay dummy client.

    If <target irc object> is specified, returns the UID of the pseudoclient
    representing the original user on the target network, similar to what
    getRemoteUser() does."""
    # First, iterate over everyone!
    try:
        remoteuser = irc.users[user].remote
    except (AttributeError, KeyError):
        remoteuser = None
    log.debug('(%s) getLocalUser: remoteuser set to %r (looking up %s/%s).', irc.name, remoteuser, user, irc.name)
    if remoteuser:
        # If targetirc is given, we'll return simply the UID of the user on the
        # target network, if it exists. Otherwise, we'll return a tuple
        # with the home network name and the original user's UID.
        sourceobj = world.networkobjects.get(remoteuser[0])
        if targetirc and sourceobj:
            if remoteuser[0] == targetirc.name:
                # The user we found's home network happens to be the one being
                # requested; just return the UID then.
                return remoteuser[1]
            # Otherwise, use getRemoteUser to find our UID.
            res = getRemoteUser(sourceobj, targetirc, remoteuser[1], spawnIfMissing=False)
            log.debug('(%s) getLocalUser: targetirc found, getting %r as remoteuser for %r (looking up %s/%s).', irc.name, res, remoteuser[1], user, irc.name)
            return res
        else:
            return remoteuser

def findRelay(chanpair):
    if chanpair in db:  # This chanpair is a shared channel; others link to it
        return chanpair
    # This chanpair is linked *to* a remote channel
    for name, dbentry in db.items():
        if chanpair in dbentry['links']:
            return name

def findRemoteChan(irc, remoteirc, channel):
    query = (irc.name, channel)
    remotenetname = remoteirc.name
    chanpair = findRelay(query)
    if chanpair is None:
        return
    if chanpair[0] == remotenetname:
        return chanpair[1]
    else:
        for link in db[chanpair]['links']:
            if link[0] == remotenetname:
                return link[1]

def initializeChannel(irc, channel):
    # We're initializing a relay that already exists. This can be done at
    # ENDBURST, or on the LINK command.
    relay = findRelay((irc.name, channel))
    log.debug('(%s) initializeChannel being called on %s', irc.name, channel)
    log.debug('(%s) initializeChannel: relay pair found to be %s', irc.name, relay)
    queued_users = []
    if relay:
        all_links = db[relay]['links'].copy()
        all_links.update((relay,))
        log.debug('(%s) initializeChannel: all_links: %s', irc.name, all_links)
        # Iterate over all the remote channels linked in this relay.
        for link in all_links:
            remotenet, remotechan = link
            if remotenet == irc.name:
                continue
            remoteirc = world.networkobjects.get(remotenet)
            if remoteirc is None:
                continue
            rc = remoteirc.channels[remotechan]
            if not (remoteirc.connected.is_set() and findRemoteChan(remoteirc, irc, remotechan)):
                continue  # They aren't connected, don't bother!
            # Join their (remote) users and set their modes.
            relayJoins(remoteirc, remotechan, rc.users, rc.ts)
            topic = remoteirc.channels[remotechan].topic
            # Only update the topic if it's different from what we already have,
            # and topic bursting is complete.
            if remoteirc.channels[remotechan].topicset and topic != irc.channels[channel].topic:
                irc.proto.topicServer(getRemoteSid(irc, remoteirc), channel, topic)
        # Send our users and channel modes to the other nets
        log.debug('(%s) initializeChannel: joining our (%s) users: %s', irc.name, remotenet, irc.channels[channel].users)
        relayJoins(irc, channel, irc.channels[channel].users, irc.channels[channel].ts)
        irc.proto.joinClient(irc.pseudoclient.uid, channel)

def handle_join(irc, numeric, command, args):
    channel = args['channel']
    if not findRelay((irc.name, channel)):
        # No relay here, return.
        return
    ts = args['ts']
    users = set(args['users'])
    relayJoins(irc, channel, users, ts, burst=False)
utils.add_hook(handle_join, 'JOIN')

def handle_quit(irc, numeric, command, args):
    for netname, user in relayusers[(irc.name, numeric)].copy().items():
        remoteirc = world.networkobjects[netname]
        remoteirc.proto.quitClient(user, args['text'])
    del relayusers[(irc.name, numeric)]
utils.add_hook(handle_quit, 'QUIT')

def handle_squit(irc, numeric, command, args):
    users = args['users']
    target = args['target']
    # Someone /SQUIT one of our relay subservers. Bad! Rejoin them!
    if target in relayservers[irc.name].values():
        sname = args['name']
        remotenet = sname.split('.', 1)[0]
        del relayservers[irc.name][remotenet]
        for userpair in relayusers:
            if userpair[0] == remotenet and irc.name in relayusers[userpair]:
                del relayusers[userpair][irc.name]
        remoteirc = world.networkobjects[remotenet]
        initializeAll(remoteirc)
    else:
        # Some other netsplit happened on the network, we'll have to fake
        # some *.net *.split quits for that.
        for user in users:
            log.debug('(%s) relay handle_squit: sending handle_quit on %s', irc.name, user)
            handle_quit(irc, user, command, {'text': '*.net *.split'})
utils.add_hook(handle_squit, 'SQUIT')

def handle_nick(irc, numeric, command, args):
    for netname, user in relayusers[(irc.name, numeric)].items():
        remoteirc = world.networkobjects[netname]
        newnick = normalizeNick(remoteirc, irc.name, args['newnick'], uid=user)
        if remoteirc.users[user].nick != newnick:
            remoteirc.proto.nickClient(user, newnick)
utils.add_hook(handle_nick, 'NICK')

def handle_part(irc, numeric, command, args):
    channels = args['channels']
    text = args['text']
    # Don't allow the PyLink client PARTing to be relayed.
    if numeric == irc.pseudoclient.uid:
        return
    for channel in channels:
        for netname, user in relayusers[(irc.name, numeric)].copy().items():
            remoteirc = world.networkobjects[netname]
            remotechan = findRemoteChan(irc, remoteirc, channel)
            if remotechan is None:
                continue
            remoteirc.proto.partClient(user, remotechan, text)
            if not remoteirc.users[user].channels:
                remoteirc.proto.quitClient(user, 'Left all shared channels.')
                del relayusers[(irc.name, numeric)][remoteirc.name]
utils.add_hook(handle_part, 'PART')

def handle_privmsg(irc, numeric, command, args):
    notice = (command == 'NOTICE')
    target = args['target']
    text = args['text']
    if target == irc.pseudoclient.uid:
        return
    relay = findRelay((irc.name, target))
    remoteusers = relayusers[(irc.name, numeric)]
    # HACK: Don't break on sending to @#channel or similar.
    try:
        prefix, target = target.split('#', 1)
    except ValueError:
        prefix = ''
    else:
        target = '#' + target
    log.debug('(%s) relay privmsg: prefix is %r, target is %r', irc.name, prefix, target)
    if utils.isChannel(target) and relay and numeric not in irc.channels[target].users:
        # The sender must be in the target channel to send messages over the relay;
        # it's the only way we can make sure they have a spawned client on ALL
        # of the linked networks. This affects -n channels too; see
        # https://github.com/GLolol/PyLink/issues/91 for an explanation of why.
        irc.msg(numeric, 'Error: You must be in %r in order to send '
                  'messages over the relay.' % target, notice=True)
        return
    if utils.isChannel(target):
        for netname, user in relayusers[(irc.name, numeric)].items():
            remoteirc = world.networkobjects[netname]
            real_target = findRemoteChan(irc, remoteirc, target)
            if not real_target:
                continue
            real_target = prefix + real_target
            if notice:
                remoteirc.proto.noticeClient(user, real_target, text)
            else:
                remoteirc.proto.messageClient(user, real_target, text)
    else:
        remoteuser = getLocalUser(irc, target)
        if remoteuser is None:
            return
        homenet, real_target = remoteuser
        # For PMs, we must be on a common channel with the target.
        # Otherwise, the sender doesn't have a client representing them
        # on the remote network, and we won't have anything to send our
        # messages from.
        if homenet not in remoteusers.keys():
            irc.msg(numeric, 'Error: You must be in a common channel '
                      'with %r in order to send messages.' % \
                      irc.users[target].nick, notice=True)
            return
        remoteirc = world.networkobjects[homenet]
        user = getRemoteUser(irc, remoteirc, numeric, spawnIfMissing=False)
        if notice:
            remoteirc.proto.noticeClient(user, real_target, text)
        else:
            remoteirc.proto.messageClient(user, real_target, text)
utils.add_hook(handle_privmsg, 'PRIVMSG')
utils.add_hook(handle_privmsg, 'NOTICE')

def checkClaim(irc, channel, sender):
    """
    Checks whether the sender of a kick/mode change passes CLAIM checks for
    a given channel. This returns True if any of the following criteria are met:

    1) The originating network is in the CLAIM list for the relay in question.
    2) The sender is halfop or above in the channel.
    3) The sender is a PyLink client (checks are suppressed in this case).
    """
    relay = findRelay((irc.name, channel))
    sender_modes = getPrefixModes(irc, irc, channel, sender)
    return irc.name in db[relay]['claim'] or \
        any([mode in sender_modes for mode in ('y', 'q', 'a', 'o', 'h')]) \
        or utils.isInternalClient(irc, sender)

def handle_kick(irc, source, command, args):
    channel = args['channel']
    target = args['target']
    text = args['text']
    kicker = source
    relay = findRelay((irc.name, channel))
    # Don't allow kicks to the PyLink client to be relayed.
    if relay is None or target == irc.pseudoclient.uid:
        return
    origuser = getLocalUser(irc, target)
    for name, remoteirc in world.networkobjects.items():
        if irc.name == name or not remoteirc.connected.is_set():
            continue
        remotechan = findRemoteChan(irc, remoteirc, channel)
        log.debug('(%s) Relay kick: remotechan for %s on %s is %s', irc.name, channel, name, remotechan)
        if remotechan is None:
            continue
        real_kicker = getRemoteUser(irc, remoteirc, kicker, spawnIfMissing=False)
        log.debug('(%s) Relay kick: real kicker for %s on %s is %s', irc.name, kicker, name, real_kicker)
        if not isRelayClient(irc, target):
            log.debug('(%s) Relay kick: target %s is NOT an internal client', irc.name, target)
            # Both the target and kicker are external clients; i.e.
            # they originate from the same network. We won't have
            # to filter this; the uplink IRCd will handle it appropriately,
            # and we'll just follow.
            real_target = getRemoteUser(irc, remoteirc, target, spawnIfMissing=False)
            log.debug('(%s) Relay kick: real target for %s is %s', irc.name, target, real_target)
        else:
            log.debug('(%s) Relay kick: target %s is an internal client, going to look up the real user', irc.name, target)
            real_target = getLocalUser(irc, target, targetirc=remoteirc)
            if not checkClaim(irc, channel, kicker):
                log.debug('(%s) Relay kick: kicker %s is not opped... We should rejoin the target user %s', irc.name, kicker, real_target)
                # Home network is not in the channel's claim AND the kicker is not
                # opped. We won't propograte the kick then.
                # TODO: make the check slightly more advanced: i.e. halfops can't
                # kick ops, admins can't kick owners, etc.
                modes = getPrefixModes(remoteirc, irc, remotechan, real_target)
                # Join the kicked client back with its respective modes.
                irc.proto.sjoinServer(getRemoteSid(irc, remoteirc), channel, [(modes, target)])
                if kicker in irc.users:
                    log.info('(%s) Relay claim: Blocked KICK (reason %r) from %s to relay client %s/%s on %s.',
                             irc.name, args['text'], irc.users[source].nick,
                             remoteirc.users[real_target].nick, remoteirc.name, channel)
                    irc.msg(kicker, "This channel is claimed; your kick to "
                                    "%s has been blocked because you are not "
                                    "(half)opped." % channel, notice=True)
                else:
                    log.info('(%s) Relay claim: Blocked KICK (reason %r) from server %s to relay client %s/%s on %s.',
                             irc.name, args['text'], irc.servers[source].name,
                             remoteirc.users[real_target].nick, remoteirc.name, channel)
                return

        if not real_target:
            continue
        # Propogate the kick!
        if real_kicker:
            log.debug('(%s) Relay kick: Kicking %s from channel %s via %s on behalf of %s/%s', irc.name, real_target, remotechan,real_kicker, kicker, irc.name)
            remoteirc.proto.kickClient(real_kicker,
                                       remotechan, real_target, text)
        else:
            # Kick originated from a server, or the kicker isn't in any
            # common channels with the target relay network.
            rsid = getRemoteSid(remoteirc, irc)
            log.debug('(%s) Relay kick: Kicking %s from channel %s via %s on behalf of %s/%s', irc.name, real_target, remotechan, rsid, kicker, irc.name)
            try:
                if kicker in irc.servers:
                    kname = irc.servers[kicker].name
                else:
                    kname = irc.users.get(kicker).nick
                text = "(%s/%s) %s" % (kname, irc.name, text)
            except AttributeError:
                text = "(<unknown kicker>@%s) %s" % (irc.name, text)
            remoteirc.proto.kickServer(rsid, remotechan, real_target, text)

        # If the target isn't on any channels, quit them.
        if origuser and origuser[0] != remoteirc.name and not remoteirc.users[real_target].channels:
            del relayusers[origuser][remoteirc.name]
            remoteirc.proto.quitClient(real_target, 'Left all shared channels.')

    if origuser and not irc.users[target].channels:
        del relayusers[origuser][irc.name]
        irc.proto.quitClient(target, 'Left all shared channels.')

utils.add_hook(handle_kick, 'KICK')

def handle_chgclient(irc, source, command, args):
    target = args['target']
    if args.get('newhost'):
        field = 'HOST'
        text = args['newhost']
    elif args.get('newident'):
        field = 'IDENT'
        text = args['newident']
    elif args.get('newgecos'):
        field = 'GECOS'
        text = args['newgecos']
    if field:
        for netname, user in relayusers[(irc.name, target)].items():
            remoteirc = world.networkobjects[netname]
            try:
                remoteirc.proto.updateClient(user, field, text)
            except NotImplementedError:  # IRCd doesn't support changing the field we want
                log.debug('(%s) Ignoring changing field %r of %s on %s (for %s/%s);'
                          ' remote IRCd doesn\'t support it', irc.name, field,
                          user, target, netname, irc.name)
                continue

for c in ('CHGHOST', 'CHGNAME', 'CHGIDENT'):
    utils.add_hook(handle_chgclient, c)

whitelisted_cmodes = {'admin', 'allowinvite', 'autoop', 'ban', 'banexception',
                      'blockcolor', 'halfop', 'invex', 'inviteonly', 'key',
                      'limit', 'moderated', 'noctcp', 'noextmsg', 'nokick',
                      'noknock', 'nonick', 'nonotice', 'op', 'operonly',
                      'opmoderated', 'owner', 'private', 'regonly',
                      'regmoderated', 'secret', 'sslonly', 'adminonly',
                      'stripcolor', 'topiclock', 'voice'}
whitelisted_umodes = {'bot', 'hidechans', 'hideoper', 'invisible', 'oper',
                      'regdeaf', 'u_stripcolor', 'u_noctcp', 'wallops'}
def relayModes(irc, remoteirc, sender, channel, modes=None):
    remotechan = findRemoteChan(irc, remoteirc, channel)
    log.debug('(%s) Relay mode: remotechan for %s on %s is %s', irc.name, channel, irc.name, remotechan)
    if remotechan is None:
        return
    if modes is None:
        modes = irc.channels[channel].modes
        log.debug('(%s) Relay mode: channel data for %s%s: %s', irc.name, remoteirc.name, remotechan, remoteirc.channels[remotechan])
    supported_modes = []
    log.debug('(%s) Relay mode: initial modelist for %s is %s', irc.name, channel, modes)
    for modepair in modes:
        try:
            prefix, modechar = modepair[0]
        except ValueError:
            modechar = modepair[0]
            prefix = '+'
        arg = modepair[1]
        # Iterate over every mode see whether the remote IRCd supports
        # this mode, and what its mode char for it is (if it is different).
        for name, m in irc.cmodes.items():
            supported_char = None
            if modechar == m:
                supported_char = remoteirc.cmodes.get(name)
                if supported_char is None:
                    break
                if name not in whitelisted_cmodes:
                    log.debug("(%s) Relay mode: skipping mode (%r, %r) because "
                              "it isn't a whitelisted (safe) mode for relay.",
                              irc.name, modechar, arg)
                    break
                if modechar in irc.prefixmodes:
                    # This is a prefix mode (e.g. +o). We must coerse the argument
                    # so that the target exists on the remote relay network.
                    log.debug("(%s) Relay mode: coersing argument of (%r, %r) "
                              "for network %r.",
                              irc.name, modechar, arg, remoteirc.name)
                    # If the target is a remote user, get the real target
                    # (original user).
                    arg = getLocalUser(irc, arg, targetirc=remoteirc) or \
                        getRemoteUser(irc, remoteirc, arg, spawnIfMissing=False)
                    log.debug("(%s) Relay mode: argument found as (%r, %r) "
                              "for network %r.",
                              irc.name, modechar, arg, remoteirc.name)
                    oplist = remoteirc.channels[remotechan].prefixmodes[name+'s']
                    log.debug("(%s) Relay mode: list of %ss on %r is: %s",
                              irc.name, name, remotechan, oplist)
                    if prefix == '+' and arg in oplist:
                        # Don't set prefix modes that are already set.
                        log.debug("(%s) Relay mode: skipping setting %s on %s/%s because it appears to be already set.",
                                  irc.name, name, arg, remoteirc.name)
                        break
                supported_char = remoteirc.cmodes.get(name)
            if supported_char:
                final_modepair = (prefix+supported_char, arg)
                if name in ('ban', 'banexception', 'invex') and not utils.isHostmask(arg):
                    # Don't add bans that don't match n!u@h syntax!
                    log.debug("(%s) Relay mode: skipping mode (%r, %r) because it doesn't match nick!user@host syntax.",
                              irc.name, modechar, arg)
                    break
                # Don't set modes that are already set, to prevent floods on TS6
                # where the same mode can be set infinite times.
                if prefix == '+' and final_modepair in remoteirc.channels[remotechan].modes:
                    log.debug("(%s) Relay mode: skipping setting mode (%r, %r) on %s%s because it appears to be already set.",
                              irc.name, supported_char, arg, remoteirc.name, remotechan)
                    break
                supported_modes.append(final_modepair)
    log.debug('(%s) Relay mode: final modelist (sending to %s%s) is %s', irc.name, remoteirc.name, remotechan, supported_modes)
    # Don't send anything if there are no supported modes left after filtering.
    if supported_modes:
        # Check if the sender is a user; remember servers are allowed to set modes too.
        u = getRemoteUser(irc, remoteirc, sender, spawnIfMissing=False)
        if u:
            remoteirc.proto.modeClient(u, remotechan, supported_modes)
        else:
            rsid = getRemoteSid(remoteirc, irc)
            remoteirc.proto.modeServer(rsid, remotechan, supported_modes)

def getSupportedUmodes(irc, remoteirc, modes):
    supported_modes = []
    for modepair in modes:
        try:
            prefix, modechar = modepair[0]
        except ValueError:
            modechar = modepair[0]
            prefix = '+'
        arg = modepair[1]
        for name, m in irc.umodes.items():
            supported_char = None
            if modechar == m:
                if name not in whitelisted_umodes:
                    log.debug("(%s) getSupportedUmodes: skipping mode (%r, %r) because "
                              "it isn't a whitelisted (safe) mode for relay.",
                              irc.name, modechar, arg)
                    break
                supported_char = remoteirc.umodes.get(name)
            if supported_char:
                supported_modes.append((prefix+supported_char, arg))
                break
        else:
            log.debug("(%s) getSupportedUmodes: skipping mode (%r, %r) because "
                      "the remote network (%s)'s IRCd (%s) doesn't support it.",
                      irc.name, modechar, arg, remoteirc.name,
                      remoteirc.protoname)
    return supported_modes

def handle_mode(irc, numeric, command, args):
    target = args['target']
    modes = args['modes']
    for name, remoteirc in world.networkobjects.items():
        if irc.name == name or not remoteirc.connected.is_set():
            continue
        if utils.isChannel(target):
            relayModes(irc, remoteirc, numeric, target, modes)
        else:
            # Set hideoper on remote opers, to prevent inflating
            # /lusers and various /stats
            hideoper_mode = remoteirc.umodes.get('hideoper')
            modes = getSupportedUmodes(irc, remoteirc, modes)
            if hideoper_mode:
                if ('+o', None) in modes:
                    modes.append(('+%s' % hideoper_mode, None))
                elif ('-o', None) in modes:
                    modes.append(('-%s' % hideoper_mode, None))
            remoteuser = getRemoteUser(irc, remoteirc, target, spawnIfMissing=False)
            if remoteuser and modes:
                remoteirc.proto.modeClient(remoteuser, remoteuser, modes)

utils.add_hook(handle_mode, 'MODE')

def handle_topic(irc, numeric, command, args):
    channel = args['channel']
    topic = args['topic']
    for name, remoteirc in world.networkobjects.items():
        if irc.name == name or not remoteirc.connected.is_set():
            continue

        remotechan = findRemoteChan(irc, remoteirc, channel)
        # Don't send if the remote topic is the same as ours.
        if remotechan is None or topic == remoteirc.channels[remotechan].topic:
            continue
        # This might originate from a server too.
        remoteuser = getRemoteUser(irc, remoteirc, numeric, spawnIfMissing=False)
        if remoteuser:
            remoteirc.proto.topicClient(remoteuser, remotechan, topic)
        else:
            rsid = getRemoteSid(remoteirc, irc)
            remoteirc.proto.topicServer(rsid, remotechan, topic)
utils.add_hook(handle_topic, 'TOPIC')

def handle_kill(irc, numeric, command, args):
    target = args['target']
    userdata = args['userdata']
    realuser = getLocalUser(irc, target) or userdata.__dict__.get('remote')
    log.debug('(%s) relay handle_kill: realuser is %r', irc.name, realuser)
    # Target user was remote:
    if realuser and realuser[0] != irc.name:
        # We don't allow killing over the relay, so we must respawn the affected
        # client and rejoin it to its channels.
        del relayusers[realuser][irc.name]
        if killcache.setdefault(irc.name, 0) <= 5:
            remoteirc = world.networkobjects[realuser[0]]
            for remotechan in remoteirc.channels.copy():
                localchan = findRemoteChan(remoteirc, irc, remotechan)
                if localchan:
                    modes = getPrefixModes(remoteirc, irc, localchan, realuser[1])
                    log.debug('(%s) relay handle_kill: userpair: %s, %s', irc.name, modes, realuser)
                    client = getRemoteUser(remoteirc, irc, realuser[1])
                    irc.proto.sjoinServer(getRemoteSid(irc, remoteirc), localchan, [(modes, client)])
            if userdata and numeric in irc.users:
                log.info('(%s) Relay claim: Blocked KILL (reason %r) from %s to relay client %s/%s.',
                         irc.name, args['text'], irc.users[numeric].nick,
                         remoteirc.users[realuser[1]].nick, realuser[0])
                irc.msg(numeric, "Your kill to %s has been blocked "
                                 "because PyLink does not allow killing"
                                 " users over the relay at this time." % \
                                 userdata.nick, notice=True)
            else:
                log.info('(%s) Relay claim: Blocked KILL (reason %r) from server %s to relay client %s/%s.',
                         irc.name, args['text'], irc.servers[numeric].name,
                         remoteirc.users[realuser[1]].nick, realuser[0])
        else:
            log.error('(%s) Too many kills received for target %s, aborting!',
                      irc.name, userdata.nick)
            irc.aborted.set()
        killcache[irc.name] += 1
        log.debug('killcache: %s', irc.name, killcache)

    # Target user was local.
    else:
        # IMPORTANT: some IRCds (charybdis) don't send explicit QUIT messages
        # for locally killed clients, while others (inspircd) do!
        # If we receive a user object in 'userdata' instead of None, it means
        # that the KILL hasn't been handled by a preceding QUIT message.
        if userdata:
            handle_quit(irc, target, 'KILL', {'text': args['text']})

utils.add_hook(handle_kill, 'KILL')

def isRelayClient(irc, user):
    try:
        if irc.users[user].remote:
            # Is the .remote attribute set? If so, don't relay already
            # relayed clients; that'll trigger an endless loop!
            return True
    except AttributeError:  # Nope, it isn't.
        pass
    except KeyError:  # The user doesn't exist?!?
        return True
    return False

def relayJoins(irc, channel, users, ts, burst=True):
    for name, remoteirc in world.networkobjects.items():
        queued_users = []
        if name == irc.name or not remoteirc.connected.is_set():
            # Don't relay things to their source network...
            continue
        remotechan = findRemoteChan(irc, remoteirc, channel)
        if remotechan is None:
            # If there is no link on our network for the user, don't
            # bother spawning it.
            continue
        log.debug('(%s) relayJoins: got %r for users', irc.name, users)
        for user in users.copy():
            if isRelayClient(irc, user):
                # Don't clone relay clients; that'll cause some bad, bad
                # things to happen.
                continue
            log.debug('Okay, spawning %s/%s everywhere', user, irc.name)
            assert user in irc.users, "(%s) How is this possible? %r isn't in our user database." % (irc.name, user)
            u = getRemoteUser(irc, remoteirc, user)
            # Only join users if they aren't already joined. This prevents op floods
            # on charybdis from all the SJOINing.
            if u not in remoteirc.channels[remotechan].users:
                ts = irc.channels[channel].ts
                prefixes = getPrefixModes(irc, remoteirc, channel, user)
                userpair = (prefixes, u)
                queued_users.append(userpair)
                log.debug('(%s) relayJoins: joining %s to %s%s', irc.name, userpair, remoteirc.name, remotechan)
            else:
                log.debug('(%s) relayJoins: not joining %s to %s%s; they\'re already there!', irc.name,
                          u, remoteirc.name, remotechan)
        if queued_users:
            # Burst was explicitly given, or we're trying to join multiple
            # users/someone with a prefix.
            if burst or len(queued_users) > 1 or queued_users[0][0]:
                rsid = getRemoteSid(remoteirc, irc)
                remoteirc.proto.sjoinServer(rsid, remotechan, queued_users, ts=ts)
                relayModes(irc, remoteirc, getRemoteSid(irc, remoteirc), channel, irc.channels[channel].modes)
            else:
                remoteirc.proto.joinClient(queued_users[0][1], remotechan)

def relayPart(irc, channel, user):
    for name, remoteirc in world.networkobjects.items():
        if name == irc.name or not remoteirc.connected.is_set():
            # Don't relay things to their source network...
            continue
        remotechan = findRemoteChan(irc, remoteirc, channel)
        log.debug('(%s) relayPart: looking for %s/%s on %s', irc.name, user, irc.name, remoteirc.name)
        log.debug('(%s) relayPart: remotechan found as %s', irc.name, remotechan)
        remoteuser = getRemoteUser(irc, remoteirc, user, spawnIfMissing=False)
        log.debug('(%s) relayPart: remoteuser for %s/%s found as %s', irc.name, user, irc.name, remoteuser)
        if remotechan is None or remoteuser is None:
            continue
        remoteirc.proto.partClient(remoteuser, remotechan, 'Channel delinked.')
        if isRelayClient(remoteirc, remoteuser) and not remoteirc.users[remoteuser].channels:
            remoteirc.proto.quitClient(remoteuser, 'Left all shared channels.')
            del relayusers[(irc.name, user)][remoteirc.name]

def removeChannel(irc, channel):
    if irc is None:
        return
    if channel not in map(str.lower, irc.serverdata['channels']):
        irc.proto.partClient(irc.pseudoclient.uid, channel, 'Channel delinked.')
    relay = findRelay((irc.name, channel))
    if relay:
        for user in irc.channels[channel].users.copy():
            if not isRelayClient(irc, user):
                relayPart(irc, channel, user)
            # Don't ever part the main client from any of its autojoin channels.
            else:
                if user == irc.pseudoclient.uid and channel in \
                        irc.serverdata['channels']:
                    continue
                irc.proto.partClient(user, channel, 'Channel delinked.')
                # Don't ever quit it either...
                if user != irc.pseudoclient.uid and not irc.users[user].channels:
                    remoteuser = getLocalUser(irc, user)
                    del relayusers[remoteuser][irc.name]
                    irc.proto.quitClient(user, 'Left all shared channels.')

@utils.add_cmd
def create(irc, source, args):
    """<channel>

    Creates the channel <channel> over the relay."""
    try:
        channel = utils.toLower(irc, args[0])
    except IndexError:
        irc.msg(source, "Error: Not enough arguments. Needs 1: channel.")
        return
    if not utils.isChannel(channel):
        irc.msg(source, 'Error: Invalid channel %r.' % channel)
        return
    if source not in irc.channels[channel].users:
        irc.msg(source, 'Error: You must be in %r to complete this operation.' % channel)
        return
    if not utils.isOper(irc, source):
        irc.msg(source, 'Error: You must be opered in order to complete this operation.')
        return
    db[(irc.name, channel)] = {'claim': [irc.name], 'links': set(), 'blocked_nets': set()}
    initializeChannel(irc, channel)
    irc.msg(source, 'Done.')

@utils.add_cmd
def destroy(irc, source, args):
    """<channel>

    Removes <channel> from the relay, delinking all networks linked to it."""
    try:
        channel = utils.toLower(irc, args[0])
    except IndexError:
        irc.msg(source, "Error: Not enough arguments. Needs 1: channel.")
        return
    if not utils.isChannel(channel):
        irc.msg(source, 'Error: Invalid channel %r.' % channel)
        return
    if not utils.isOper(irc, source):
        irc.msg(source, 'Error: You must be opered in order to complete this operation.')
        return

    entry = (irc.name, channel)
    if entry in db:
        for link in db[entry]['links']:
            removeChannel(world.networkobjects.get(link[0]), link[1])
        removeChannel(irc, channel)
        del db[entry]
        irc.msg(source, 'Done.')
    else:
        irc.msg(source, 'Error: No such relay %r exists.' % channel)
        return

@utils.add_cmd
def link(irc, source, args):
    """<remotenet> <channel> <local channel>

    Links channel <channel> on <remotenet> over the relay to <local channel>.
    If <local channel> is not specified, it defaults to the same name as <channel>."""
    try:
        channel = utils.toLower(irc, args[1])
        remotenet = args[0].lower()
    except IndexError:
        irc.msg(source, "Error: Not enough arguments. Needs 2-3: remote netname, channel, local channel name (optional).")
        return
    try:
        localchan = utils.toLower(irc, args[2])
    except IndexError:
        localchan = channel
    for c in (channel, localchan):
        if not utils.isChannel(c):
            irc.msg(source, 'Error: Invalid channel %r.' % c)
            return
    if source not in irc.channels[localchan].users:
        irc.msg(source, 'Error: You must be in %r to complete this operation.' % localchan)
        return
    if not utils.isOper(irc, source):
        irc.msg(source, 'Error: You must be opered in order to complete this operation.')
        return
    if remotenet not in world.networkobjects:
        irc.msg(source, 'Error: No network named %r exists.' % remotenet)
        return
    localentry = findRelay((irc.name, localchan))
    if localentry:
        irc.msg(source, 'Error: Channel %r is already part of a relay.' % localchan)
        return
    try:
        entry = db[(remotenet, channel)]
    except KeyError:
        irc.msg(source, 'Error: No such relay %r exists.' % channel)
        return
    else:
        if irc.name in entry['blocked_nets']:
            irc.msg(source, 'Error: Access denied (network is banned from linking to this channel).')
            return
        for link in entry['links']:
            if link[0] == irc.name:
                irc.msg(source, "Error: Remote channel '%s%s' is already"
                                       " linked here as %r." % (remotenet,
                                                                channel, link[1]))
                return
        entry['links'].add((irc.name, localchan))
        initializeChannel(irc, localchan)
        irc.msg(source, 'Done.')

@utils.add_cmd
def delink(irc, source, args):
    """<local channel> [<network>]

    Delinks channel <local channel>. <network> must and can only be specified if you are on the host network for <local channel>, and allows you to pick which network to delink.
    To remove a relay entirely, use the 'destroy' command instead."""
    try:
        channel = utils.toLower(irc, args[0])
    except IndexError:
        irc.msg(source, "Error: Not enough arguments. Needs 1-2: channel, remote netname (optional).")
        return
    try:
        remotenet = args[1].lower()
    except IndexError:
        remotenet = None
    if not utils.isOper(irc, source):
        irc.msg(source, 'Error: You must be opered in order to complete this operation.')
        return
    if not utils.isChannel(channel):
        irc.msg(source, 'Error: Invalid channel %r.' % channel)
        return
    entry = findRelay((irc.name, channel))
    if entry:
        if entry[0] == irc.name:  # We own this channel.
            if not remotenet:
                irc.msg(source, "Error: You must select a network to "
                          "delink, or use the 'destroy' command to remove "
                          "this relay entirely (it was created on the current "
                          "network).")
                return
            else:
               for link in db[entry]['links'].copy():
                    if link[0] == remotenet:
                        removeChannel(world.networkobjects.get(remotenet), link[1])
                        db[entry]['links'].remove(link)
        else:
            removeChannel(irc, channel)
            db[entry]['links'].remove((irc.name, channel))
        irc.msg(source, 'Done.')
    else:
        irc.msg(source, 'Error: No such relay %r.' % channel)

def initializeAll(irc):
    log.debug('(%s) initializeAll: waiting for world.started', irc.name)
    world.started.wait()
    for chanpair, entrydata in db.items():
        network, channel = chanpair
        initializeChannel(irc, channel)
        for link in entrydata['links']:
            network, channel = link
            initializeChannel(irc, channel)

def main():
    loadDB()
    world.schedulers['relaydb'] = scheduler = sched.scheduler()
    scheduler.enter(30, 1, exportDB, argument=(True,))
    # Thread this because exportDB() queues itself as part of its
    # execution, in order to get a repeating loop.
    thread = threading.Thread(target=scheduler.run)
    thread.daemon = True
    thread.start()

def handle_endburst(irc, numeric, command, args):
    if numeric == irc.uplink:
        initializeAll(irc)
utils.add_hook(handle_endburst, "ENDBURST")

def handle_disconnect(irc, numeric, command, args):
    for k, v in relayusers.copy().items():
        if irc.name in v:
            del relayusers[k][irc.name]
        if k[0] == irc.name:
            del relayusers[k]
    for name, ircobj in world.networkobjects.items():
        if name != irc.name:
            rsid = getRemoteSid(ircobj, irc)
            ircobj.proto.squitServer(ircobj.sid, rsid, text='Home network lost connection.')
            del relayservers[name][irc.name]
    del relayservers[irc.name]
    # handle_quit(irc, k[1], 'PYLINK_DISCONNECT', {'text': 'Home network lost connection.'})

utils.add_hook(handle_disconnect, "PYLINK_DISCONNECT")

def handle_save(irc, numeric, command, args):
    target = args['target']
    realuser = getLocalUser(irc, target)
    log.debug('(%s) relay handle_save: %r got in a nick collision! Real user: %r',
                  irc.name, target, realuser)
    if isRelayClient(irc, target) and realuser:
        # Nick collision!
        # It's one of our relay clients; try to fix our nick to the next
        # available normalized nick.
        remotenet, remoteuser = realuser
        remoteirc = world.networkobjects[remotenet]
        nick = remoteirc.users[remoteuser].nick
        # Limit how many times we can attempt to fix our nick, to prevent
        # floods and such.
        if savecache.setdefault(irc.name, 0) <= 5:
            newnick = normalizeNick(irc, remotenet, nick)
            log.info('(%s) SAVE received for relay client %r (%s), fixing nick to %s',
                      irc.name, target, nick, newnick)
            irc.proto.nickClient(target, newnick)
        else:
            log.warning('(%s) SAVE received for relay client %r (%s), not '
                        'fixing nick again due to 5 failed attempts in '
                        'the last 10 seconds!', irc.name, target, nick)
        savecache[irc.name] += 1
    else:
        # Somebody else on the network (not a PyLink client) had a nick collision;
        # relay this as a nick change appropriately.
        handle_nick(irc, target, 'SAVE', {'oldnick': None, 'newnick': target})

utils.add_hook(handle_save, "SAVE")

@utils.add_cmd
def linked(irc, source, args):
    """takes no arguments.

    Returns a list of channels shared across the relay."""
    networks = list(world.networkobjects.keys())
    networks.remove(irc.name)
    s = 'Connected networks: \x02%s\x02 %s' % (irc.name, ' '.join(networks))
    irc.msg(source, s)
    # Sort relay DB by channel name, and then sort.
    for k, v in sorted(db.items(), key=lambda channel: channel[0][1]):
        s = '\x02%s%s\x02 ' % k
        if v['links']:
            s += ' '.join([''.join(link) for link in v['links']])
        else:
            s += '(no relays yet)'
        irc.msg(source, s)

def handle_away(irc, numeric, command, args):
    for netname, user in relayusers[(irc.name, numeric)].items():
        remoteirc = world.networkobjects[netname]
        remoteirc.proto.awayClient(user, args['text'])
utils.add_hook(handle_away, 'AWAY')

def handle_spawnmain(irc, numeric, command, args):
    if args['olduser']:
        # Kills to the main PyLink client force reinitialization; this makes sure
        # it joins all the relay channels like it's supposed to.
        initializeAll(irc)
utils.add_hook(handle_spawnmain, 'PYLINK_SPAWNMAIN')

def handle_invite(irc, source, command, args):
    target = args['target']
    channel = args['channel']
    if isRelayClient(irc, target):
        remotenet, remoteuser = getLocalUser(irc, target)
        remoteirc = world.networkobjects[remotenet]
        remotechan = findRemoteChan(irc, remoteirc, channel)
        remotesource = getRemoteUser(irc, remoteirc, source, spawnIfMissing=False)
        if remotesource is None:
            irc.msg(source, 'Error: You must be in a common channel '
                                   'with %s to invite them to channels.' % \
                                   irc.users[target].nick,
                                   notice=True)
        elif remotechan is None:
            irc.msg(source, 'Error: You cannot invite someone to a '
                                   'channel not on their network!',
                                   notice=True)
        else:
            remoteirc.proto.inviteClient(remotesource, remoteuser,
                                         remotechan)
utils.add_hook(handle_invite, 'INVITE')

@utils.add_cmd
def linkacl(irc, source, args):
    """ALLOW|DENY|LIST <channel> <remotenet>

    Allows blocking / unblocking certain networks from linking to a relay, based on a blacklist.
    LINKACL LIST returns a list of blocked networks for a channel, while the ALLOW and DENY subcommands allow manipulating this blacklist."""
    missingargs = "Error: Not enough arguments. Needs 2-3: subcommand (ALLOW/DENY/LIST), channel, remote network (for ALLOW/DENY)."
    if not utils.isOper(irc, source):
        irc.msg(source, 'Error: You must be opered in order to complete this operation.')
        return
    try:
        cmd = args[0].lower()
        channel = utils.toLower(irc, args[1])
    except IndexError:
        irc.msg(source, missingargs)
        return
    if not utils.isChannel(channel):
        irc.msg(source, 'Error: Invalid channel %r.' % channel)
        return
    relay = findRelay((irc.name, channel))
    if not relay:
        irc.msg(source, 'Error: No such relay %r exists.' % channel)
        return
    if cmd == 'list':
        s = 'Blocked networks for \x02%s\x02: \x02%s\x02' % (channel, ', '.join(db[relay]['blocked_nets']) or '(empty)')
        irc.msg(source, s)
        return

    try:
        remotenet = args[2]
    except IndexError:
        irc.msg(source, missingargs)
        return
    if cmd == 'deny':
        db[relay]['blocked_nets'].add(remotenet)
        irc.msg(source, 'Done.')
    elif cmd == 'allow':
        try:
            db[relay]['blocked_nets'].remove(remotenet)
        except KeyError:
            irc.msg(source, 'Error: Network %r is not on the blacklist for %r.' % (remotenet, channel))
        else:
            irc.msg(source, 'Done.')
    else:
        irc.msg(source, 'Error: Unknown subcommand %r: valid ones are ALLOW, DENY, and LIST.' % cmd)

@utils.add_cmd
def showuser(irc, source, args):
    """<user>

    Shows relay data about user <user>. This is intended to be used alongside the 'commands' plugin, which provides a 'showuser' command with more general information."""
    try:
        target = args[0]
    except IndexError:
        # No errors here; showuser from the commands plugin already does this
        # for us.
        return
    u = utils.nickToUid(irc, target)
    if u:
        try:
            userpair = getLocalUser(irc, u) or (irc.name, u)
            remoteusers = relayusers[userpair].items()
        except KeyError:
            pass
        else:
            nicks = []
            if remoteusers:
                nicks.append('%s:\x02%s\x02' % (userpair[0],
                             world.networkobjects[userpair[0]].users[userpair[1]].nick))
                for r in remoteusers:
                    remotenet, remoteuser = r
                    remoteirc = world.networkobjects[remotenet]
                    nicks.append('%s:\x02%s\x02' % (remotenet, remoteirc.users[remoteuser].nick))
                irc.msg(source, "\x02Relay nicks\x02: %s" % ', '.join(nicks))
        relaychannels = []
        for ch in irc.users[u].channels:
            relay = findRelay((irc.name, ch))
            if relay:
                relaychannels.append(''.join(relay))
        if relaychannels and (utils.isOper(irc, source) or u == source):
            irc.msg(source, "\x02Relay channels\x02: %s" % ' '.join(relaychannels))
