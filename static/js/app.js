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
    if (Notification.permission === 'default') {
      await Notification.requestPermission();
    }
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
      body: JSON.stringify(subscription)
    });
  } catch (error) {
    console.debug('push subscription unavailable', error);
  }
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
  const renderedIds = new Set();
  let autoRefreshScheduled = false;

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
    if (wasHidden) {
      fetchNotifications(true);
    }
  });

  if ('Notification' in window && Notification.permission === 'default') {
    Notification.requestPermission();
  }

  const isUserTyping = () => {
    const active = document.activeElement;
    if (!active) return false;
    const tag = active.tagName;
    return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT';
  };

  const acknowledgeNotifications = async () => {
    try {
      await fetch('/api/notifications?mark_read=1', { credentials: 'same-origin' });
    } catch (error) {
      console.debug('ack notifications failed', error);
    }
  };

  const scheduleAutoRefresh = async () => {
    if (autoRefreshScheduled) {
      return;
    }
    if (document.visibilityState !== 'visible') {
      return;
    }
    if (isUserTyping()) {
      return;
    }

    autoRefreshScheduled = true;
    await acknowledgeNotifications();
    setTimeout(() => {
      window.location.reload();
    }, 800);
  };

  const renderNotifications = (items, markRead = false) => {
    updateBadge(items.length);

    if (!items.length) {
      notifyEmpty.style.display = 'block';
      return;
    }
    notifyEmpty.style.display = 'none';

    for (const item of items) {
      if (renderedIds.has(item.id)) {
        continue;
      }
      renderedIds.add(item.id);

      const row = document.createElement('div');
      row.className = 'list-item block';

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
      notifyList.prepend(row);

      if (swRegistration && 'Notification' in window && Notification.permission === 'granted') {
        swRegistration.showNotification(document.title.replace(/\s-\s.*/, ''), {
          body: item.message,
          tag: `pocketkid-${item.id}`,
          renotify: false,
          icon: '/static/icons/logo-192.png',
          badge: '/static/icons/logo-192.png'
        });
      }
    }

    if (!markRead) {
      scheduleAutoRefresh();
    }
  };

  const fetchNotifications = async (markRead = false) => {
    try {
      const url = markRead ? '/api/notifications?mark_read=1' : '/api/notifications';
      const response = await fetch(url, { credentials: 'same-origin' });
      if (!response.ok) return;
      const data = await response.json();
      if (Array.isArray(data.items)) {
        renderNotifications(data.items, markRead);
        if (markRead) {
          updateBadge(0);
        }
      }
    } catch (error) {
      console.debug('notifications unavailable', error);
    }
  };

  fetchNotifications();
  setInterval(fetchNotifications, 15000);
}
