/* exported init */

const {Gio, GLib, St} = imports.gi;
const Main = imports.ui.main;

const APP_BUS_NAME = 'io.github.focustomato.FocusTomato';
const STATE_OBJECT_PATH = '/io/github/focustomato/FocusTomato/State';
const BAND_HEIGHT = 3; // GNOME logical pixels, not raw panel pixels.
const STATE_POLL_SECONDS = 3;

const StateProxy = Gio.DBusProxy.makeProxyWrapper(`
<node>
  <interface name="io.github.focustomato.FocusTomato.State">
    <method name="GetState">
      <arg name="status" type="s" direction="out"/>
      <arg name="break_kind" type="s" direction="out"/>
    </method>
    <signal name="StateChanged">
      <arg name="status" type="s"/>
      <arg name="break_kind" type="s"/>
    </signal>
  </interface>
</node>`);

const COLORS = {
    focus: '#b85c57',
    shortBreak: '#63946f',
    longBreak: '#5f79a8',
    paused: '#b58a45',
};

class FocusTomatoStateBandExtension {
    constructor() {
        this._band = null;
        this._watchId = 0;
        this._proxy = null;
        this._proxySignalId = 0;
        this._proxyGeneration = 0;
        this._stateRevision = 0;
        this._pollId = 0;
        this._monitorChangedId = 0;
        this._panelAllocationId = 0;
        this._fullscreenId = 0;
        this._state = 'idle';
        this._breakKind = '';
    }

    enable() {
        this._band = new St.Widget({
            name: 'focusTomatoStateBand',
            reactive: false,
            can_focus: false,
            opacity: 255,
        });
        this._band.hide();

        // Overlay the bottom three logical pixels of the existing GNOME panel.
        // It creates neither a strut nor an input region, so window geometry and
        // pointer interaction remain unchanged.
        Main.layoutManager.addChrome(this._band, {
            affectsStruts: false,
            affectsInputRegion: false,
            trackFullscreen: true,
        });

        this._monitorChangedId = Main.layoutManager.connect(
            'monitors-changed', () => this._updateGeometry());
        this._panelAllocationId = Main.panel.connect(
            'notify::allocation', () => this._updateGeometry());
        this._fullscreenId = global.display.connect(
            'in-fullscreen-changed', () => this._applyState());

        this._updateGeometry();
        this._watchId = Gio.bus_watch_name(
            Gio.BusType.SESSION,
            APP_BUS_NAME,
            Gio.BusNameWatcherFlags.NONE,
            this._onNameAppeared.bind(this),
            this._onNameVanished.bind(this));
    }

    disable() {
        this._proxyGeneration += 1;
        if (this._watchId) {
            Gio.bus_unwatch_name(this._watchId);
            this._watchId = 0;
        }
        this._disconnectProxy();
        if (this._monitorChangedId) {
            Main.layoutManager.disconnect(this._monitorChangedId);
            this._monitorChangedId = 0;
        }
        if (this._panelAllocationId) {
            Main.panel.disconnect(this._panelAllocationId);
            this._panelAllocationId = 0;
        }
        if (this._fullscreenId) {
            global.display.disconnect(this._fullscreenId);
            this._fullscreenId = 0;
        }
        if (this._band) {
            Main.layoutManager.removeChrome(this._band);
            this._band.destroy();
            this._band = null;
        }
    }

    _onNameAppeared(_connection, _name, owner) {
        this._proxyGeneration += 1;
        const generation = this._proxyGeneration;
        this._disconnectProxy();
        const proxy = new StateProxy(
            Gio.DBus.session,
            APP_BUS_NAME,
            STATE_OBJECT_PATH,
            (readyProxy, error) => {
                if (error || generation !== this._proxyGeneration ||
                    readyProxy !== this._proxy)
                    return;
                this._proxySignalId = readyProxy.connectSignal(
                    'StateChanged', (_source, _sender, [status, breakKind]) => {
                        if (generation !== this._proxyGeneration ||
                            readyProxy !== this._proxy)
                            return;
                        // Invalidate every GetState reply that was requested
                        // before this newer signal arrived.
                        this._stateRevision += 1;
                        this._setState(status, breakKind);
                    });
                this._requestState(readyProxy, generation);
                this._pollId = GLib.timeout_add_seconds(
                    GLib.PRIORITY_DEFAULT,
                    STATE_POLL_SECONDS,
                    () => {
                        if (generation !== this._proxyGeneration ||
                            readyProxy !== this._proxy) {
                            this._pollId = 0;
                            return GLib.SOURCE_REMOVE;
                        }
                        this._requestState(readyProxy, generation);
                        return GLib.SOURCE_CONTINUE;
                    });
            });
        this._proxy = proxy;
        log(`[Focus Tomato state band] connected to ${owner}`);
    }

    _onNameVanished() {
        this._proxyGeneration += 1;
        this._disconnectProxy();
        this._setState('idle', '');
        log('[Focus Tomato state band] application disappeared; hiding band');
    }

    _disconnectProxy() {
        if (this._pollId) {
            GLib.source_remove(this._pollId);
            this._pollId = 0;
        }
        if (this._proxy && this._proxySignalId)
            this._proxy.disconnectSignal(this._proxySignalId);
        this._proxySignalId = 0;
        this._proxy = null;
    }

    _requestState(proxy, generation) {
        const requestedAtRevision = this._stateRevision;
        proxy.GetStateRemote((result, error) => {
            if (error || !result || generation !== this._proxyGeneration ||
                proxy !== this._proxy ||
                requestedAtRevision !== this._stateRevision)
                return;
            this._setState(result[0], result[1]);
        });
    }

    _setState(status, breakKind) {
        const knownStates = [
            'idle',
            'focus_running',
            'focus_paused',
            'break_ready',
            'break_running',
            'break_paused',
        ];
        const nextState = knownStates.includes(status) ? status : 'idle';
        const nextBreakKind = breakKind || '';
        if (nextState === this._state && nextBreakKind === this._breakKind)
            return;
        this._state = nextState;
        this._breakKind = nextBreakKind;
        log(`[Focus Tomato state band] state=${this._state} break=${this._breakKind || '-'}`);
        this._applyState();
    }

    _colorForState() {
        if (this._state === 'focus_running')
            return COLORS.focus;
        if (this._state === 'break_running')
            return this._breakKind === 'long' ? COLORS.longBreak : COLORS.shortBreak;
        if (this._state === 'focus_paused' || this._state === 'break_paused')
            return COLORS.paused;
        return null;
    }

    _applyState() {
        if (!this._band)
            return;
        const color = this._colorForState();
        const primaryIndex = Main.layoutManager.primaryIndex;
        const fullscreen = primaryIndex >= 0 &&
            global.display.get_monitor_in_fullscreen(primaryIndex);
        if (!color || fullscreen) {
            this._band.hide();
            return;
        }
        this._band.set_style(`background-color: ${color};`);
        this._updateGeometry();
        this._band.show();
    }

    _updateGeometry() {
        if (!this._band || !Main.layoutManager.primaryMonitor)
            return;
        const monitor = Main.layoutManager.primaryMonitor;
        const panelHeight = Math.max(BAND_HEIGHT, Math.round(Main.panel.height));
        this._band.set_position(
            Math.round(monitor.x),
            Math.round(monitor.y + panelHeight - BAND_HEIGHT));
        this._band.set_size(Math.round(monitor.width), BAND_HEIGHT);
    }
}

function init() {
    return new FocusTomatoStateBandExtension();
}
