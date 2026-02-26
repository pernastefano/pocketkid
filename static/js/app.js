if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/static/sw.js');
  });
}

let swRegistration = null;
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.ready.then((registration) => {
    swRegistration = registration;
    setupWebPushSubscription();
  });
}

const base64ToUint8Array = (base64String) => {
  const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
  const rawData = window.atob(base64);
  return Uint8Array.from([...rawData].map((ch) => ch.charCodeAt(0)));
};

const setupWebPushSubscription = async () => {
  if (!swRegistration || !('PushManager' in window) || !('Notification' in window)) {
    return;
  }

  try {
    if (Notification.permission !== 'granted') {
      return;
    }

    const keyResponse = await fetch('/api/push/public-key', { credentials: 'same-origin' });
    if (!keyResponse.ok) {
      return;
    }
    const keyData = await keyResponse.json();
    if (!keyData.publicKey) {
      return;
    }

    let subscription = await swRegistration.pushManager.getSubscription();
    if (!subscription) {
      subscription = await swRegistration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: base64ToUint8Array(keyData.publicKey)
      });
    }

    await fetch('/api/push/subscribe', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(subscription.toJSON())
    });
  } catch (error) {
    console.debug('push subscription unavailable', error);
  }
};

const ensurePushPermissionAndSubscribe = async () => {
  if (!('Notification' in window)) {
    return;
  }

  try {
    if (Notification.permission === 'default') {
      const result = await Notification.requestPermission();
      if (result !== 'granted') {
        return;
      }
    }
    await setupWebPushSubscription();
  } catch (error) {
    console.debug('push permission unavailable', error);
  }
};

let pushPromptBound = false;
const bindFirstInteractionPushPrompt = () => {
  if (pushPromptBound) {
    return;
  }
  pushPromptBound = true;

  const tryOnce = async () => {
    window.removeEventListener('click', tryOnce, true);
    window.removeEventListener('touchstart', tryOnce, true);
    await ensurePushPermissionAndSubscribe();
  };

  window.addEventListener('click', tryOnce, { capture: true, passive: true });
  window.addEventListener('touchstart', tryOnce, { capture: true, passive: true });
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
    ensurePushPermissionAndSubscribe();
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

      if (
        swRegistration &&
        'Notification' in window &&
        Notification.permission === 'granted' &&
        !item.is_read &&
        !shownNotificationIds.has(item.id)
      ) {
        shownNotificationIds.add(item.id);
        swRegistration.showNotification(document.title.replace(/\s-\s.*/, ''), {
          body: item.message,
          tag: `pocketkid-${item.id}`,
          renotify: false,
          icon: '/static/icons/logo-192.png',
          badge: '/static/icons/logo-192.png'
        });
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
    } else if (Notification.permission === 'default') {
      bindFirstInteractionPushPrompt();
    }
  }
  setInterval(fetchNotifications, 15000);
}
