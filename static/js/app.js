document.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-copy-target]');
  if (!button) return;
  const targetId = button.getAttribute('data-copy-target');
  const field = document.getElementById(targetId);
  if (!field) return;
  try {
    await navigator.clipboard.writeText(field.value || field.textContent || '');
    const old = button.textContent;
    button.textContent = 'Copiado ✅';
    setTimeout(() => { button.textContent = old; }, 1200);
  } catch (err) {
    field.select();
    document.execCommand('copy');
  }
});

function splitTags(value) {
  return (value || '')
    .split(',')
    .map(tag => tag.trim().toLowerCase())
    .filter(Boolean);
}

function appendTag(input, tag) {
  if (!input || !tag) return;
  const current = splitTags(input.value);
  const normalized = tag.trim().toLowerCase();
  if (!normalized || current.includes(normalized)) {
    input.focus();
    return;
  }
  current.push(normalized);
  input.value = current.join(', ');
  input.dispatchEvent(new Event('input', { bubbles: true }));
  input.focus();
}

function currentTagFragment(value) {
  const parts = (value || '').split(',');
  return (parts[parts.length - 1] || '').trim().toLowerCase();
}

function refreshSuggestionVisibility(input, container) {
  if (!input || !container) return;
  const used = new Set(splitTags(input.value));
  const needle = currentTagFragment(input.value);
  const buttons = [...container.querySelectorAll('[data-tag-value]')];
  let visible = 0;
  buttons.forEach(button => {
    const tag = (button.getAttribute('data-tag-value') || '').toLowerCase();
    const matchesNeedle = !needle || tag.includes(needle);
    const shouldShow = !used.has(tag) && matchesNeedle;
    button.hidden = !shouldShow;
    if (shouldShow) visible += 1;
  });
  container.classList.toggle('vf-tag-suggestions-empty', visible === 0 && buttons.length > 0);
}

document.querySelectorAll('[data-tag-suggestions-for]').forEach(container => {
  const input = document.getElementById(container.getAttribute('data-tag-suggestions-for'));
  if (!input) return;

  container.addEventListener('click', (event) => {
    const button = event.target.closest('[data-tag-value]');
    if (!button) return;
    appendTag(input, button.getAttribute('data-tag-value'));
  });

  input.addEventListener('input', () => refreshSuggestionVisibility(input, container));
  refreshSuggestionVisibility(input, container);
});

function playMatchSound() {
  const AudioContextClass = window.AudioContext || window.webkitAudioContext;
  if (!AudioContextClass) return Promise.resolve(false);

  const ctx = new AudioContextClass();
  const now = ctx.currentTime;
  const gain = ctx.createGain();
  gain.gain.setValueAtTime(0.0001, now);
  gain.gain.exponentialRampToValueAtTime(0.12, now + 0.02);
  gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.45);
  gain.connect(ctx.destination);

  const first = ctx.createOscillator();
  first.type = 'sine';
  first.frequency.setValueAtTime(740, now);
  first.frequency.exponentialRampToValueAtTime(920, now + 0.16);
  first.connect(gain);
  first.start(now);
  first.stop(now + 0.22);

  const second = ctx.createOscillator();
  second.type = 'sine';
  second.frequency.setValueAtTime(1180, now + 0.16);
  second.connect(gain);
  second.start(now + 0.16);
  second.stop(now + 0.42);

  return new Promise(resolve => {
    setTimeout(() => {
      ctx.close().catch(() => {});
      resolve(true);
    }, 520);
  });
}

function triggerMatchSoundOnce(key, force = false) {
  if (!key && !force) return;
  const storageKey = 'vendeFacilLastMatchSound';
  if (!force && sessionStorage.getItem(storageKey) === key) return;

  const markPlayed = () => {
    if (!force) sessionStorage.setItem(storageKey, key);
  };

  playMatchSound()
    .then(markPlayed)
    .catch(() => {
      const playAfterGesture = () => {
        playMatchSound().finally(markPlayed);
        window.removeEventListener('pointerdown', playAfterGesture);
        window.removeEventListener('keydown', playAfterGesture);
      };
      window.addEventListener('pointerdown', playAfterGesture, { once: true });
      window.addEventListener('keydown', playAfterGesture, { once: true });
    });
}

const matchSoundAlert = document.querySelector('[data-vf-match-sound="1"]');
if (matchSoundAlert) {
  window.setTimeout(() => {
    triggerMatchSoundOnce(matchSoundAlert.getAttribute('data-vf-match-sound-key') || 'match');
  }, 500);
}

document.addEventListener('click', (event) => {
  const testButton = event.target.closest('[data-vf-test-match-sound]');
  if (!testButton) return;
  triggerMatchSoundOnce(`test-${Date.now()}`, true);
});
