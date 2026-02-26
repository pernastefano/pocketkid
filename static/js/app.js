// Push notification preference key
const PUSH_PREF_KEY = 'pocketkid_push_pref';
const PUSH_PREF_ENABLED = 'enabled';
const PUSH_PREF_DISABLED = 'disabled';
const PUSH_PREF_UNSET = 'unset';

// Get user's push notification preference
const getPushPreference = () => {
  try {
    return localStorage.getItem(PUSH_PREF_KEY) || PUSH_PREF_UNSET;
  } catch (e) {
    return PUSH_PREF_UNSET;
  }
};

// Set user's push notification preference
const setPushPreference = (value) => {
  try {
    localStorage.setItem(PUSH_PREF_KEY, value);
  } catch (e) {
    console.debug('Cannot save push preference', e);
  }
};

if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch((err) => {
      console.debug('Service worker registration failed', err);
    });
  });
}

let swRegistration = null;
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.ready.then((registration) => {
    swRegistration = registration;
    // Auto-setup if user has already granted permission
    if ('Notification' in window && Notification.permission === 'granted') {
      setupWebPushSubscription();
    }
  });
}

const base64ToUint8Array = (base64String) => {
  const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
  const rawData = window.atob(base64);
  return Uint8Array.from([...rawData].map((ch) => ch.charCodeAt(0)));
};

const uint8ArrayToBase64Url = (buffer) => {
  if (!buffer) {
    return '';
  }

  const bytes = new Uint8Array(buffer);
  let binary = '';
  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }

  return window.btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/g, '');
};

const serializePushSubscription = (subscription) => {
  const json = subscription && subscription.toJSON ? subscription.toJSON() : {};
  const p256dh = (json.keys && json.keys.p256dh) || uint8ArrayToBase64Url(subscription.getKey && subscription.getKey('p256dh'));
  const auth = (json.keys && json.keys.auth) || uint8ArrayToBase64Url(subscription.getKey && subscription.getKey('auth'));

  return {
    endpoint: json.endpoint || subscription.endpoint,
    keys: {
      p256dh,
      auth
    }
  };
};

const setupWebPushSubscription = async () => {
  if (!swRegistration || !('PushManager' in window) || !('Notification' in window)) {
    return false;
  }
  if (!window.isSecureContext) {
    console.debug('push subscription unavailable: insecure context');
    return false;
  }

  try {
    if (Notification.permission !== 'granted') {
      return false;
    }

    const keyResponse = await fetch('/api/push/public-key', { credentials: 'same-origin' });
    if (!keyResponse.ok) {
      return false;
    }
    const keyData = await keyResponse.json();
    if (!keyData.publicKey) {
      return false;
    }

    let subscription = await swRegistration.pushManager.getSubscription();
    if (!subscription) {
      subscription = await swRegistration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: base64ToUint8Array(keyData.publicKey)
      });
    }

    const subscribeResponse = await fetch('/api/push/subscribe', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(serializePushSubscription(subscription))
    });
    if (!subscribeResponse.ok) {
      const responseText = await subscribeResponse.text();
      throw new Error(`push subscribe failed: ${subscribeResponse.status} ${responseText}`);
    }
    
    setPushPreference(PUSH_PREF_ENABLED);
    return true;
  } catch (error) {
    console.debug('push subscription unavailable', error);
    return false;
  }
};

const unsubscribeWebPush = async () => {
  if (!swRegistration || !('PushManager' in window)) {
    return false;
  }

  try {
    const subscription = await swRegistration.pushManager.getSubscription();
    if (subscription) {
      await subscription.unsubscribe();
      
      await fetch('/api/push/unsubscribe', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ endpoint: subscription.endpoint })
      }).catch(() => {});
    }
    
    setPushPreference(PUSH_PREF_DISABLED);
    return true;
  } catch (error) {
    console.debug('unsubscribe failed', error);
    return false;
  }
};

const ensurePushPermissionAndSubscribe = async (force = false) => {
  if (!('Notification' in window)) {
    return false;
  }

  try {
    // If user previously disabled, don't ask again unless forced
    const pref = getPushPreference();
    if (!force && pref === PUSH_PREF_DISABLED) {
      return false;
    }

    if (Notification.permission === 'default') {
      const result = await Notification.requestPermission();
      if (result !== 'granted') {
        setPushPreference(PUSH_PREF_DISABLED);
        return false;
      }
    } else if (Notification.permission === 'denied') {
      setPushPreference(PUSH_PREF_DISABLED);
      return false;
    }
    
    return await setupWebPushSubscription();
  } catch (error) {
    console.debug('push permission unavailable', error);
    return false;
  }
};

const ensureServerPushSubscription = async () => {
  if (!swRegistration || !('Notification' in window) || Notification.permission !== 'granted') {
    return;
  }

  try {
    const response = await fetch('/api/push/debug', { credentials: 'same-origin' });
    if (!response.ok) {
      return;
    }

    const data = await response.json();
    const active = Number(data.activeSubscriptions || 0);
    if (active === 0) {
      await setupWebPushSubscription();
    }
  } catch (error) {
    console.debug('push debug unavailable', error);
  }
};

let pushPromptBound = false;
const bindFirstInteractionPushPrompt = () => {
  if (pushPromptBound) {
    return;
  }
  
  const pref = getPushPreference();
  // Don't bind if user has explicitly disabled or enabled
  if (pref !== PUSH_PREF_UNSET) {
    return;
  }
  
  pushPromptBound = true;

  const tryOnce = async () => {
    window.removeEventListener('click', tryOnce, true);
    window.removeEventListener('touchstart', tryOnce, true);
    await ensurePushPermissionAndSubscribe(false);
  };

  window.addEventListener('click', tryOnce, { capture: true, passive: true, once: true });
  window.addEventListener('touchstart', tryOnce, { capture: true, passive: true, once: true });
};

if ('serviceWorker' in navigator) {
  navigator.serviceWorker.addEventListener('message', (event) => {
    if (!event.data || event.data.type !== 'PUSH_EVENT') {
      return;
    }
    setTimeout(() => {
      window.location.reload();
    }, 500);
  });
}

document.querySelectorAll('form[data-double-confirm="true"]').forEach((form) => {
  form.addEventListener('submit', (event) => {
    if (form.dataset.confirmed === '1') {
      return;
    }
    event.preventDefault();
    const first = form.dataset.confirmFirst || 'Confirm?';
    const second = form.dataset.confirmSecond || 'Final confirmation?';
    if (!window.confirm(first)) {
      return;
    }
    if (!window.confirm(second)) {
      return;
    }
    const hidden = form.querySelector('input[name="double_confirmed"]');
    if (hidden) {
      hidden.value = '1';
    }
    form.dataset.confirmed = '1';
    form.submit();
  });
});

document.querySelectorAll('form[data-deposit-mode-form="true"]').forEach((form) => {
  const movementSelect = form.querySelector('select[name="movement"]');
  const depositModeSelect = form.querySelector('select[name="deposit_mode"]');
  const depositOnlyFields = form.querySelectorAll('[data-deposit-only="true"]');
  const challengeFields = form.querySelectorAll('[data-challenge-field="true"]');

  if (!movementSelect || !depositModeSelect) {
    return;
  }

  const syncFieldVisibility = () => {
    const isDeposit = movementSelect.value === 'deposit';
    const isChallengeMode = depositModeSelect.value === 'challenge';

    depositOnlyFields.forEach((node) => {
      node.classList.toggle('hidden', !isDeposit);
    });

    challengeFields.forEach((node) => {
      node.classList.toggle('hidden', !(isDeposit && isChallengeMode));
      const select = node.querySelector('select[name="challenge_id"]');
      if (select && !(isDeposit && isChallengeMode)) {
        select.value = '';
      }
    });
  };

  movementSelect.addEventListener('change', syncFieldVisibility);
  depositModeSelect.addEventListener('change', syncFieldVisibility);
  syncFieldVisibility();
});

const requestToggleButtons = document.querySelectorAll('[data-request-toggle]');
const requestForms = document.querySelectorAll('[data-request-form]');

if (requestToggleButtons.length && requestForms.length) {
  const showRequestForm = (formName) => {
    const activeButton = document.querySelector('[data-request-toggle].active');
    if (activeButton && activeButton.dataset.requestToggle === formName) {
      requestForms.forEach((section) => section.classList.add('hidden'));
      requestToggleButtons.forEach((btn) => btn.classList.remove('active'));
      return;
    }

    requestForms.forEach((section) => {
      section.classList.toggle('hidden', section.dataset.requestForm !== formName);
    });
    requestToggleButtons.forEach((btn) => {
      btn.classList.toggle('active', btn.dataset.requestToggle === formName);
    });
  };

  requestToggleButtons.forEach((btn) => {
    btn.addEventListener('click', () => showRequestForm(btn.dataset.requestToggle));
  });
}

const notifyToggle = document.getElementById('notify-toggle');
const notifyPanel = document.getElementById('notify-panel');
const notifyList = document.getElementById('notify-list');
const notifyEmpty = document.getElementById('notify-empty');
const notifyBadge = document.getElementById('notify-badge');

if (notifyToggle && notifyPanel && notifyList && notifyEmpty && notifyBadge) {
  let fetchSequence = 0;
  let latestAppliedSequence = 0;
  const shownNotificationIds = new Set();
  const lastSeenStorageKey = 'pk_last_seen_notification_id';

  const getLastSeenNotificationId = () => {
    const raw = window.sessionStorage.getItem(lastSeenStorageKey);
    const parsed = Number(raw || 0);
    return Number.isFinite(parsed) ? parsed : 0;
  };

  const setLastSeenNotificationId = (id) => {
    if (!Number.isFinite(id) || id <= 0) {
      return;
    }
    window.sessionStorage.setItem(lastSeenStorageKey, String(id));
  };

  const updateBadge = (count) => {
    notifyBadge.textContent = String(count);
    if (count > 0) {
      notifyBadge.classList.remove('hidden');
    } else {
      notifyBadge.classList.add('hidden');
    }
  };

  notifyToggle.addEventListener('click', () => {
    const wasHidden = notifyPanel.classList.contains('hidden');
    notifyPanel.classList.toggle('hidden');
    
    // Auto-request permission when opening notifications panel if not already decided
    if (wasHidden && getPushPreference() === PUSH_PREF_UNSET) {
      ensurePushPermissionAndSubscribe(false);
    }
    
    if (wasHidden) {
      fetchNotifications(true);
    }
  });

  const renderNotifications = (items, unreadCount = 0) => {
    updateBadge(unreadCount);
    notifyList.innerHTML = '';

    if (!items.length) {
      notifyEmpty.style.display = 'block';
      return;
    }
    notifyEmpty.style.display = 'none';

    for (const item of items) {
      const row = document.createElement('div');
      row.className = 'list-item block';
      if (item.is_read) {
        row.classList.add('is-read');
      }

      const wrapper = document.createElement('div');
      const label = document.createElement('p');
      label.className = 'label';
      label.textContent = item.message;
      const time = document.createElement('p');
      time.className = 'muted';
      time.textContent = item.created_at;

      wrapper.appendChild(label);
      wrapper.appendChild(time);
      row.appendChild(wrapper);
      notifyList.appendChild(row);

      if (!item.is_read && !shownNotificationIds.has(item.id)) {
        shownNotificationIds.add(item.id);
      }
    }

    const latestId = Number(items[0] && items[0].id ? items[0].id : 0);
    const isDashboardView = window.location.pathname === '/dashboard';
    const panelHidden = notifyPanel.classList.contains('hidden');
    const lastSeenId = getLastSeenNotificationId();

    if (
      isDashboardView &&
      panelHidden &&
      document.visibilityState === 'visible' &&
      latestId > lastSeenId &&
      unreadCount > 0
    ) {
      setLastSeenNotificationId(latestId);
      setTimeout(() => {
        window.location.reload();
      }, 500);
      return;
    }

    if (latestId > 0) {
      setLastSeenNotificationId(latestId);
    }
  };

  const fetchNotifications = async (markRead = false) => {
    const sequence = ++fetchSequence;

    try {
      const url = markRead ? '/api/notifications?mark_read=1' : '/api/notifications';
      const response = await fetch(url, { credentials: 'same-origin' });
      if (!response.ok) return;
      const data = await response.json();
      if (sequence < latestAppliedSequence) {
        return;
      }
      latestAppliedSequence = sequence;
      if (Array.isArray(data.items)) {
        renderNotifications(data.items, Number(data.unreadCount || 0));
      }
    } catch (error) {
      console.debug('notifications unavailable', error);
    }
  };

  fetchNotifications();
  if ('Notification' in window) {
    if (Notification.permission === 'granted') {
      setupWebPushSubscription();
      ensureServerPushSubscription();
    } else if (Notification.permission === 'default' && getPushPreference() === PUSH_PREF_UNSET) {
      bindFirstInteractionPushPrompt();
    }
  }
  setInterval(fetchNotifications, 15000);
  setInterval(ensureServerPushSubscription, 45000);
}

const pushToggleButton = document.getElementById('push-toggle-btn');
const pushRegisterButton = document.getElementById('push-register-btn');
const pushRegisterStatus = document.getElementById('push-register-status');

if (pushToggleButton && pushRegisterButton && pushRegisterStatus) {
  const updatePushUI = async () => {
    const pref = getPushPreference();
    const permission = ('Notification' in window) ? Notification.permission : 'denied';
    
    let statusText = '';
    let isEnabled = false;
    
    if (permission === 'granted' && pref === PUSH_PREF_ENABLED) {
      try {
        const response = await fetch('/api/push/debug', { credentials: 'same-origin' });
        if (response.ok) {
          const data = await response.json();
          const active = Number(data.activeSubscriptions || 0);
          if (active > 0) {
            statusText = `✓ Attive (${active} subscription${active > 1 ? 's' : ''})`;
            isEnabled = true;
          } else {
            statusText = '⚠ Permesso concesso, attesa registrazione...';
          }
        } else {
          statusText = '⚠ Impossibile verificare lo stato';
        }
      } catch (e) {
        statusText = '⚠ Errore di connessione';
      }
    } else if (permission === 'denied') {
      statusText = '✗ Bloccate dal browser (controlla impostazioni browser)';
      isEnabled = false;
    } else if (pref === PUSH_PREF_DISABLED) {
      statusText = '○ Disattivate (clicca per attivare)';
      isEnabled = false;
    } else {
      statusText = '○ Non configurate (clicca per attivare)';
      isEnabled = false;
    }
    
    pushToggleButton.textContent = isEnabled ? 'Disattiva Notifiche Push' : 'Attiva Notifiche Push';
    pushToggleButton.className = isEnabled ? 'btn btn-warning' : 'btn';
    pushRegisterStatus.textContent = statusText;
    pushRegisterStatus.classList.toggle('error-text', permission === 'denied');
  };

  pushToggleButton.addEventListener('click', async () => {
    pushToggleButton.disabled = true;
    const pref = getPushPreference();
    const permission = ('Notification' in window) ? Notification.permission : 'denied';
    
    try {
      if (permission === 'granted' && pref === PUSH_PREF_ENABLED) {
        // Disable notifications
        await unsubscribeWebPush();
        pushRegisterStatus.textContent = '○ Notifiche disattivate';
      } else {
        // Enable notifications
        const success = await ensurePushPermissionAndSubscribe(true);
        if (success) {
          pushRegisterStatus.textContent = '✓ Notifiche attivate con successo';
        } else {
          pushRegisterStatus.textContent = '✗ Impossibile attivare le notifiche';
          pushRegisterStatus.classList.add('error-text');
        }
      }
      
      setTimeout(updatePushUI, 500);
    } catch (error) {
      pushRegisterStatus.textContent = '✗ Errore durante l\'operazione';
      pushRegisterStatus.classList.add('error-text');
      console.debug('push toggle failed', error);
    } finally {
      pushToggleButton.disabled = false;
    }
  });

  pushRegisterButton.addEventListener('click', async () => {
    pushRegisterButton.disabled = true;
    pushRegisterStatus.textContent = 'Verifica in corso...';

    try {
      await ensurePushPermissionAndSubscribe(true);
      setTimeout(updatePushUI, 500);
    } catch (error) {
      pushRegisterStatus.textContent = '✗ Verifica fallita';
      pushRegisterStatus.classList.add('error-text');
      console.debug('manual push registration failed', error);
    } finally {
      pushRegisterButton.disabled = false;
    }
  });
  
  updatePushUI();
}
