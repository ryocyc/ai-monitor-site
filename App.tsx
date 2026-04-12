import AsyncStorage from '@react-native-async-storage/async-storage';
import * as Notifications from 'expo-notifications';
import { StatusBar } from 'expo-status-bar';
import { useEffect, useState } from 'react';
import { Alert, LogBox, Platform, Pressable, ScrollView, StyleSheet, Text, TextInput, View } from 'react-native';
import { SafeAreaProvider, SafeAreaView } from 'react-native-safe-area-context';
import { translations, type SupportedLocale, weekDayKeys, type WeekDayKey } from './src/i18n';
import { saveDevicePushToken, saveFamilyMemberPushToken, isFirebaseReady, sendRealPushNotification, isValidExpoPushToken } from './src/pushService';

LogBox.ignoreLogs([
  'expo-notifications: Android Push notifications (remote notifications) functionality provided by expo-notifications was removed from Expo Go',
  '`expo-notifications` functionality is not fully supported in Expo Go',
]);

type ScreenKey = 'today' | 'medications' | 'history' | 'family' | 'settings';

type MealTiming = 'before' | 'after' | 'none';

type TakenSlot = {
  date: string;
  doseTime: string;
};

type MedicationItem = {
  id: string;
  name: string;
  dose: string;
  time: string;
  note: string;
  startDate: string;
  endDate: string;
  repeatDays: WeekDayKey[];
  historyDates: string[];
  createdAt: string;
  intervalHours: number;
  mealTiming: MealTiming;
  isShared: boolean;
  sharedWithMemberIds: string[];
  takenSlots: TakenSlot[];
};

type DraftMedication = {
  name: string;
  dose: string;
  time: string;
  note: string;
  startDate: string;
  endDate: string;
  repeatDays: WeekDayKey[];
  intervalHours: number;
  mealTiming: MealTiming;
  isShared: boolean;
  sharedWithMemberIds: string[];
};

type NotificationState = Record<string, { signature: string; ids: string[] }>;

type FamilyMember = {
  id: string;
  name: string;
  relationship: string;
  status: 'online' | 'offline';
  lastSeen: string;
  pushToken?: string;
};

type QuickReminder = {
  id: string;
  medicationName: string;
  medicationDose: string;
  recipientIds: string[];
  sentAt: string;
  status: 'sent' | 'delivered' | 'failed';
};

type PermissionLevel = 'view' | 'edit' | 'admin';

type MedicationShare = {
  medicationId: string;
  isShared: boolean;
  sharedWith: {
    memberId: string;
    permission: PermissionLevel;
  }[];
};

const SHARING_STORAGE_KEY = 'medreminder.sharing.v1';
const FAMILY_STORAGE_KEY = 'medreminder.family.v1';
const REMINDERS_STORAGE_KEY = 'medreminder.reminders.v1';
const RECEIVED_STORAGE_KEY = 'medreminder.received.v1';
const PUSH_STORAGE_KEY = 'medreminder.push.v1';

const STORAGE_KEY = 'medreminder.medications.v8';
const NOTIFICATION_STATE_KEY = 'medreminder.notifications.v1';
const NOTIFICATION_LOOKAHEAD_DAYS = 60;

const starterItems: MedicationItem[] = [
  {
    id: 'starter-1',
    name: 'Metformin',
    dose: '500 mg',
    time: '08:00',
    note: 'Take after breakfast',
    startDate: '2026-04-10',
    endDate: '2026-05-10',
    repeatDays: [...weekDayKeys],
    historyDates: [],
    createdAt: '2026-04-10T08:00:00.000Z',
    intervalHours: 24,
    mealTiming: 'after',
    isShared: false,
    sharedWithMemberIds: [],
    takenSlots: [],
  },
  {
    id: 'starter-2',
    name: 'Vitamin D3',
    dose: '1 tablet',
    time: '12:30',
    note: 'Lunch supplement',
    startDate: '2026-04-10',
    endDate: '2026-06-10',
    repeatDays: ['mon', 'wed', 'fri'],
    historyDates: [],
    createdAt: '2026-04-10T12:30:00.000Z',
    intervalHours: 24,
    mealTiming: 'none',
    isShared: false,
    sharedWithMemberIds: [],
    takenSlots: [],
  },
];

Notifications.setNotificationHandler({
  handleNotification: async () => ({
    shouldShowBanner: true,
    shouldShowList: true,
    shouldPlaySound: true,
    shouldSetBadge: false,
  }),
});

function createDraft(): DraftMedication {
  return {
    name: '',
    dose: '',
    time: '',
    note: '',
    startDate: '',
    endDate: '',
    repeatDays: [...weekDayKeys],
    intervalHours: 24,
    mealTiming: 'none',
    isShared: false,
    sharedWithMemberIds: [],
  };
}

function createDraftFromItem(item: MedicationItem): DraftMedication {
  return {
    name: item.name,
    dose: item.dose,
    time: item.time,
    note: item.note,
    startDate: item.startDate,
    endDate: item.endDate,
    repeatDays: [...item.repeatDays],
    intervalHours: item.intervalHours,
    mealTiming: item.mealTiming,
    isShared: item.isShared,
    sharedWithMemberIds: item.sharedWithMemberIds,
  };
}

function normalizeTimeInput(value: string) {
  return value.replace(/[^0-9:]/g, '').slice(0, 5);
}

function normalizeDateInput(value: string) {
  return value.replace(/[^0-9-]/g, '').slice(0, 10);
}

function isValidTime(value: string) {
  return /^([01]\d|2[0-3]):([0-5]\d)$/.test(value);
}

function isValidDate(value: string) {
  return /^\d{4}-\d{2}-\d{2}$/.test(value);
}

function sortByTime(items: MedicationItem[]) {
  return [...items].sort((a, b) => a.time.localeCompare(b.time));
}

function migrateHistoryToSlots(historyDates: string[], time: string, intervalHours: number): { date: string; doseTime: string }[] {
  const slots: { date: string; doseTime: string }[] = [];
  const { hour, minute } = parseTimeParts(time || '08:00');
  const interval = intervalHours || 24;
  for (const date of historyDates) {
    for (let offsetHours = 0; offsetHours < 24; offsetHours += interval) {
      const doseHour = (hour + offsetHours) % 24;
      slots.push({
        date,
        doseTime: `${String(doseHour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`,
      });
    }
  }
  return slots;
}

function parseTimeParts(time: string) {
  const [h = '0', m = '0'] = time.split(':');
  return { hour: parseInt(h, 10) || 0, minute: parseInt(m, 10) || 0 };
}

function parseDate(date: string) {
  const [y, mo, d] = date.split('-').map((v) => parseInt(v, 10) || 0);
  return { year: y, month: mo - 1, day: d };
}

function toLocalDate(dateStr: string): Date {
  const { year, month, day } = parseDate(dateStr);
  return new Date(year, month, day);
}

function formatDateKey(date: Date): string {
  const y = date.getFullYear();
  const mo = String(date.getMonth() + 1).padStart(2, '0');
  const d = String(date.getDate()).padStart(2, '0');
  return `${y}-${mo}-${d}`;
}

function addDays(date: Date, days: number): Date {
  const result = new Date(date);
  result.setDate(result.getDate() + days);
  return result;
}

function getWeekDayKeyFromDate(date: Date): WeekDayKey {
  const keys: WeekDayKey[] = ['sun', 'mon', 'tue', 'wed', 'thu', 'fri', 'sat'];
  return keys[date.getDay()];
}

type DoseSlot = { item: MedicationItem; doseTime: string };

function getDoseTimesForDay(item: MedicationItem, date: Date): string[] {
  const { hour, minute } = parseTimeParts(item.time);
  const intervalHours = item.intervalHours || 24;
  const times: string[] = [];
  if (intervalHours >= 24) {
    const intervalDays = Math.ceil(intervalHours / 24);
    const startDaysFromEpoch = Math.floor(toLocalDate(item.startDate).getTime() / (24 * 60 * 60 * 1000));
    const dateDaysFromEpoch = Math.floor(date.getTime() / (24 * 60 * 60 * 1000));
    if ((dateDaysFromEpoch - startDaysFromEpoch) % intervalDays === 0) {
      times.push(`${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`);
    }
  } else {
    for (let offsetHours = 0; offsetHours < 24; offsetHours += intervalHours) {
      const doseHour = (hour + offsetHours) % 24;
      times.push(`${String(doseHour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`);
    }
  }
  return times;
}

function isSlotTaken(item: MedicationItem, date: string, doseTime: string) {
  return item.takenSlots.some((slot) => slot.date === date && slot.doseTime === doseTime);
}

function areAllSlotsTakenForDay(item: MedicationItem, date: string) {
  if (!item.repeatDays.includes(getWeekDayKeyFromDate(new Date(date)))) return false;
  const doseTimes = getDoseTimesForDay(item, new Date(date));
  return doseTimes.every((dt) => isSlotTaken(item, date, dt));
}

function isAnySlotTakenForDay(item: MedicationItem, date: string) {
  return item.takenSlots.some((slot) => slot.date === date);
}

function isTakenOnDate(item: MedicationItem, date: string) {
  return areAllSlotsTakenForDay(item, date);
}

function buildNotificationDates(item: MedicationItem, fromDate: Date) {
  const start = toLocalDate(item.startDate);
  const end = toLocalDate(item.endDate);
  const windowStart = start > fromDate ? start : fromDate;
  const windowEnd = addDays(fromDate, NOTIFICATION_LOOKAHEAD_DAYS);
  const finalEnd = end < windowEnd ? end : windowEnd;
  const results: Date[] = [];
  const { hour, minute } = parseTimeParts(item.time);
  const intervalHours = item.intervalHours || 24;

  if (intervalHours >= 24) {
    const intervalDays = Math.ceil(intervalHours / 24);
    const startDaysFromEpoch = Math.floor(start.getTime() / (24 * 60 * 60 * 1000));
    for (let cursor = new Date(windowStart); cursor <= finalEnd; cursor = addDays(cursor, 1)) {
      if (!item.repeatDays.includes(getWeekDayKeyFromDate(cursor))) continue;
      const cursorDaysFromEpoch = Math.floor(cursor.getTime() / (24 * 60 * 60 * 1000));
      if ((cursorDaysFromEpoch - startDaysFromEpoch) % intervalDays !== 0) continue;
      const scheduledAt = new Date(cursor.getFullYear(), cursor.getMonth(), cursor.getDate(), hour, minute, 0, 0);
      const doseTimeStr = `${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`;
      if (!item.takenSlots.some((slot) => slot.date === formatDateKey(cursor) && slot.doseTime === doseTimeStr) && scheduledAt > new Date()) {
        results.push(scheduledAt);
      }
    }
  } else {
    for (let cursor = new Date(windowStart); cursor <= finalEnd; cursor = addDays(cursor, 1)) {
      if (!item.repeatDays.includes(getWeekDayKeyFromDate(cursor))) continue;
      for (let offsetHours = 0; offsetHours < 24; offsetHours += intervalHours) {
        const doseHour = (hour + offsetHours) % 24;
        const doseTime = `${String(doseHour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`;
        if (item.takenSlots.some((slot) => slot.date === formatDateKey(cursor) && slot.doseTime === doseTime)) continue;
        const scheduledAt = new Date(cursor.getFullYear(), cursor.getMonth(), cursor.getDate(), doseHour, minute, 0, 0);
        if (scheduledAt > new Date()) results.push(scheduledAt);
      }
    }
  }

  return results;
}

function buildNotificationSignature(item: MedicationItem) {
  return `${item.name}|${item.dose}|${item.time}|${item.intervalHours}|${item.mealTiming}|${item.startDate}|${item.endDate}|${item.repeatDays.join(',')}`;
}

export default function App() {
  const locale: SupportedLocale = 'en';
  const copy = translations[locale];
  const familyUpdates = [copy.family1, copy.family2, copy.family3];
  const [screen, setScreen] = useState<ScreenKey>('today');
  const [medications, setMedications] = useState<MedicationItem[]>(starterItems);
  const [draft, setDraft] = useState<DraftMedication>(createDraft());
  const [editingId, setEditingId] = useState<string | null>(null);
  const [isLoaded, setIsLoaded] = useState(false);
  const [notificationPermission, setNotificationPermission] = useState<'unknown' | 'granted' | 'denied'>('unknown');
  const [familyMembers, setFamilyMembers] = useState<FamilyMember[]>([]);
  const [quickReminders, setQuickReminders] = useState<QuickReminder[]>([]);
  const [medicationSharing, setMedicationSharing] = useState<MedicationShare[]>([]);
  const [reminderForm, setReminderForm] = useState<{ medicationId: string; recipientIds: string[] } | null>(null);
  const [editingMemberId, setEditingMemberId] = useState<string | null>(null);
  const [editingMemberName, setEditingMemberName] = useState('');
  const [editingMemberRelation, setEditingMemberRelation] = useState('');
  const [lastNotificationSync, setLastNotificationSync] = useState<string | null>(null);
  const [wifiState, setWifiState] = useState<'idle' | 'scanning' | 'connected'>('idle');
  const [wifiDevices, setWifiDevices] = useState<{ id: string; name: string }[]>([]);
  const [connectedDevice, setConnectedDevice] = useState<{ id: string; name: string } | null>(null);
  const [ocrResult, setOcrResult] = useState<{ name: string; dose: string; note: string } | null>(null);
  const [ocrLoading, setOcrLoading] = useState(false);
  const [showMedForm, setShowMedForm] = useState(false);
  const [receivedReminders, setReceivedReminders] = useState<{ id: string; fromName: string; medicationName: string; medicationDose: string; receivedAt: string; status: 'new' | 'acknowledged' }[]>([]);
  const [pushToken, setPushToken] = useState<string | null>(null);
  const [pushRegistrationStatus, setPushRegistrationStatus] = useState<'idle' | 'registering' | 'registered' | 'failed'>('idle');
  const [firebaseReady, setFirebaseReady] = useState(false);
  const [editingMemberPushToken, setEditingMemberPushToken] = useState('');

  useEffect(() => {
    async function configureNotifications() {
      if (Platform.OS !== 'android') return;
      await Notifications.setNotificationChannelAsync('medication-reminders', {
        name: 'Medication reminders',
        importance: Notifications.AndroidImportance.MAX,
        vibrationPattern: [0, 250, 250, 250],
        lightColor: '#183A37',
      });
    }
    configureNotifications();
    async function loadMedications() {
      try {
        const saved = await AsyncStorage.getItem(STORAGE_KEY);
        if (saved) {
          const parsed = JSON.parse(saved) as MedicationItem[];
          const migrated = parsed.map((item) => {
            if (item.takenSlots && item.takenSlots.length > 0) return item;
            if (item.historyDates && item.historyDates.length > 0) {
              return { ...item, takenSlots: migrateHistoryToSlots(item.historyDates, item.time, item.intervalHours || 24) };
            }
            return item;
          });
          setMedications(sortByTime(migrated));
        }
        setIsLoaded(true);
      } catch (error) {
        console.error('Failed to load medications', error);
        setIsLoaded(true);
      }
    }
    loadMedications();
    async function checkNotificationPermission() {
      const { status } = await Notifications.getPermissionsAsync();
      setNotificationPermission(status === 'granted' ? 'granted' : 'denied');
    }
    async function loadFamilyData() {
      try {
        const [familyData, remindersData, sharingData, receivedData, pushData] = await Promise.all([
          AsyncStorage.getItem(FAMILY_STORAGE_KEY),
          AsyncStorage.getItem(REMINDERS_STORAGE_KEY),
          AsyncStorage.getItem(SHARING_STORAGE_KEY),
          AsyncStorage.getItem(RECEIVED_STORAGE_KEY),
          AsyncStorage.getItem(PUSH_STORAGE_KEY),
        ]);
        if (familyData) {
          const parsed = JSON.parse(familyData) as FamilyMember[];
          setFamilyMembers(parsed.filter((m) => m.id && m.name));
        }
        if (pushData) {
          const parsed = JSON.parse(pushData) as { token?: string; status?: string };
          if (parsed.token) setPushToken(parsed.token);
          if (parsed.status) setPushRegistrationStatus(parsed.status as 'idle' | 'registering' | 'registered' | 'failed');
        }
        if (remindersData) {
          const parsed = JSON.parse(remindersData) as QuickReminder[];
          setQuickReminders(parsed.filter((r) => r.recipientIds.length > 0));
        }
        if (sharingData) {
          const parsed = JSON.parse(sharingData) as MedicationShare[];
          setMedicationSharing(parsed.filter((s) => s.isShared === true || s.sharedWith.length > 0));
        }
        if (receivedData) {
          const parsed = JSON.parse(receivedData) as { id: string; fromName: string; medicationName: string; medicationDose: string; receivedAt: string; status: 'new' | 'acknowledged' }[];
          setReceivedReminders(parsed);
        }
      } catch (error) {
        console.error('Failed to load family data', error);
      }
    }
    checkNotificationPermission();
    loadFamilyData();
    setFirebaseReady(isFirebaseReady());
  }, []);

  useEffect(() => {
    if (!isLoaded) return;
    AsyncStorage.setItem(STORAGE_KEY, JSON.stringify(medications)).catch((error) => {
      console.error('Failed to save medications', error);
    });
    AsyncStorage.setItem(FAMILY_STORAGE_KEY, JSON.stringify(familyMembers)).catch((error) => {
      console.error('Failed to save family members', error);
    });
    const validReminders = quickReminders.filter((r) => r.recipientIds.length > 0);
    AsyncStorage.setItem(REMINDERS_STORAGE_KEY, JSON.stringify(validReminders)).catch((error) => {
      console.error('Failed to save quick reminders', error);
    });
    const validSharing = medicationSharing.filter((s) => s.isShared === true || s.sharedWith.length > 0);
    AsyncStorage.setItem(SHARING_STORAGE_KEY, JSON.stringify(validSharing)).catch((error) => {
      console.error('Failed to save medication sharing', error);
    });
    AsyncStorage.setItem(RECEIVED_STORAGE_KEY, JSON.stringify(receivedReminders)).catch((error) => {
      console.error('Failed to save received reminders', error);
    });
    AsyncStorage.setItem(PUSH_STORAGE_KEY, JSON.stringify({ token: pushToken, status: pushRegistrationStatus })).catch((error) => {
      console.error('Failed to save push registration', error);
    });
  }, [isLoaded, medications, familyMembers, quickReminders, medicationSharing, receivedReminders, pushToken, pushRegistrationStatus]);

  useEffect(() => {
    if (!isLoaded) return;

    async function syncNotifications() {
      const current = await AsyncStorage.getItem(NOTIFICATION_STATE_KEY);
      const state: NotificationState = current ? JSON.parse(current) as NotificationState : {};
      const permissions = await Notifications.getPermissionsAsync();
      const granted = permissions.granted ? permissions : await Notifications.requestPermissionsAsync();
      if (!granted.granted) return;

      const nextState: NotificationState = {};
      const activeIds = new Set(medications.map((item) => item.id));
      const todayStart = toLocalDate(formatDateKey(new Date()));

      for (const [itemId, entry] of Object.entries(state)) {
        if (activeIds.has(itemId)) continue;
        await Promise.all(entry.ids.map((id) => Notifications.cancelScheduledNotificationAsync(id).catch(() => undefined)));
      }

      for (const item of medications) {
        if (!isValidTime(item.time) || !isValidDate(item.startDate) || !isValidDate(item.endDate)) continue;
        const signature = buildNotificationSignature(item);
        const existing = state[item.id];

        if (existing?.signature === signature) {
          nextState[item.id] = existing;
          continue;
        }

        if (existing) {
          await Promise.all(existing.ids.map((id) => Notifications.cancelScheduledNotificationAsync(id).catch(() => undefined)));
        }

        const ids: string[] = [];
        for (const scheduledAt of buildNotificationDates(item, todayStart)) {
          try {
            const id = await Notifications.scheduleNotificationAsync({
              content: {
                title: item.name,
                body: `${item.dose}${item.note ? ` | ${item.note}` : ''}`,
                sound: true,
              },
              trigger: {
                type: Notifications.SchedulableTriggerInputTypes.DATE,
                date: scheduledAt,
              },
            });
            ids.push(id);
          } catch (error) {
            console.error('Failed to schedule notification', error);
          }
        }
        nextState[item.id] = { signature, ids };
      }

      await AsyncStorage.setItem(NOTIFICATION_STATE_KEY, JSON.stringify(nextState));
      const syncTime = new Date().toISOString();
      setLastNotificationSync(syncTime);
    }

    syncNotifications();
  }, [isLoaded, medications]);

  function updateDraft<K extends keyof DraftMedication>(key: K, value: DraftMedication[K]) {
    setDraft((current) => ({ ...current, [key]: value }));
  }

  function toggleDraftDay(day: WeekDayKey) {
    setDraft((current) => ({
      ...current,
      repeatDays: current.repeatDays.includes(day)
        ? current.repeatDays.filter((item) => item !== day)
        : [...current.repeatDays, day],
    }));
  }

  function validateDraft() {
    const { name, dose, time, startDate, endDate, repeatDays } = draft;
    if (!name.trim()) return Alert.alert(copy.missingNameTitle, copy.missingNameMessage), false;
    if (!dose.trim()) return Alert.alert(copy.missingDoseTitle, copy.missingDoseMessage), false;
    if (!time.trim()) return Alert.alert(copy.invalidTimeTitle, copy.invalidTimeMessage), false;
    if (!startDate.trim() || !endDate.trim()) return Alert.alert(copy.invalidDateTitle, copy.invalidDateMessage), false;
    if (endDate < startDate) return Alert.alert(copy.invalidDateRangeTitle, copy.invalidDateRangeMessage), false;
    if (repeatDays.length === 0) return Alert.alert(copy.missingRepeatTitle, copy.missingRepeatMessage), false;
    if (draft.isShared && draft.sharedWithMemberIds.length === 0) return Alert.alert('Select Members', 'Select at least one family member to share with.'), false;
    return true;
  }

  function submitMedication() {
    if (!validateDraft()) return;

    const nextData = {
      name: draft.name.trim(),
      dose: draft.dose.trim(),
      time: draft.time.trim(),
      note: draft.note.trim(),
      startDate: draft.startDate.trim(),
      endDate: draft.endDate.trim(),
      repeatDays: [...draft.repeatDays].sort((a, b) => weekDayKeys.indexOf(a) - weekDayKeys.indexOf(b)),
      intervalHours: draft.intervalHours,
      mealTiming: draft.mealTiming,
      isShared: draft.isShared,
      sharedWithMemberIds: draft.sharedWithMemberIds,
    };

    if (editingId) {
      setMedications((current) => sortByTime(current.map((item) => {
        if (item.id !== editingId) return item;
        return { ...item, ...nextData };
      })));
      if (draft.isShared) {
        setMedicationSharing((current) => {
          const existing = current.find((s) => s.medicationId === editingId);
          if (existing) {
            return current.map((s) => s.medicationId === editingId ? { ...s, isShared: true, sharedWith: draft.sharedWithMemberIds.map((mid) => ({ memberId: mid, permission: 'view' as PermissionLevel })) } : s);
          }
          return [...current, { medicationId: editingId, isShared: true, sharedWith: draft.sharedWithMemberIds.map((mid) => ({ memberId: mid, permission: 'view' as PermissionLevel })) }];
        });
      } else {
        setMedicationSharing((current) => current.filter((s) => s.medicationId !== editingId));
      }
    } else {
      const nextItem: MedicationItem = {
        id: `${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
        ...nextData,
        historyDates: [],
        createdAt: new Date().toISOString(),
        takenSlots: [],
      };
      setMedications((current) => sortByTime([nextItem, ...current]));
      if (draft.isShared) {
        setMedicationSharing((current) => [...current, { medicationId: nextItem.id, isShared: true, sharedWith: draft.sharedWithMemberIds.map((mid) => ({ memberId: mid, permission: 'view' as PermissionLevel })) }]);
      }
    }

    setDraft(createDraft());
    setEditingId(null);
    setShowMedForm(false);
    setScreen('medications');
  }

  function startEditMedication(id: string) {
    const item = medications.find((entry) => entry.id === id);
    if (!item) return;
    setEditingId(id);
    setDraft(createDraftFromItem(item));
    setShowMedForm(true);
    setScreen('medications');
  }

  function toggleMedication(id: string) {
    const item = medications.find((m) => m.id === id);
    if (!item) return;
    const doseTimes = getDoseTimesForDay(item, new Date(today));
    const allTaken = doseTimes.every((dt) => isSlotTaken(item, today, dt));
    setMedications((current) =>
      sortByTime(
        current.map((m) => {
          if (m.id !== id) return m;
          if (allTaken) {
            return { ...m, takenSlots: m.takenSlots.filter((slot) => slot.date !== today) };
          } else {
            const newSlots = [...m.takenSlots];
            doseTimes.forEach((dt) => {
              if (!newSlots.some((s) => s.date === today && s.doseTime === dt)) {
                newSlots.push({ date: today, doseTime: dt });
              }
            });
            return { ...m, takenSlots: newSlots };
          }
        })
      )
    );
  }

  function toggleSlot(id: string, date: string, doseTime: string) {
    setMedications((current) =>
      sortByTime(
        current.map((m) => {
          if (m.id !== id) return m;
          const exists = m.takenSlots.some((s) => s.date === date && s.doseTime === doseTime);
          if (exists) {
            return { ...m, takenSlots: m.takenSlots.filter((s) => !(s.date === date && s.doseTime === doseTime)) };
          } else {
            return { ...m, takenSlots: [...m.takenSlots, { date, doseTime }] };
          }
        })
      )
    );
  }

  function deleteMedication(id: string) {
    Alert.alert(copy.deleteTitle, copy.deleteMessage(medications.find((m) => m.id === id)?.name || ''), [
      { text: copy.cancelButton, style: 'cancel' },
      {
        text: copy.actionDelete,
        style: 'destructive',
        onPress: () => {
          setMedications((current) => current.filter((m) => m.id !== id));
          setMedicationSharing((current) => current.filter((s) => s.medicationId !== id));
          if (editingId === id) {
            setDraft(createDraft());
            setEditingId(null);
            setShowMedForm(false);
          }
        },
      },
    ]);
  }

  function resetStarterData() {
    Alert.alert(copy.resetTitle, copy.resetMessage, [
      { text: 'Cancel', style: 'cancel' },
      {
        text: 'Reset',
        style: 'destructive',
        onPress: async () => {
          setMedications(starterItems);
          setDraft(createDraft());
          setEditingId(null);
          setShowMedForm(false);
          await AsyncStorage.removeItem(NOTIFICATION_STATE_KEY);
        },
      },
    ]);
  }

  function resyncNotifications() {
    Alert.alert('Resync Notifications', 'This will reschedule all medication notifications. Continue?', [
      { text: 'Cancel', style: 'cancel' },
      {
        text: 'Resync',
        onPress: async () => {
          await AsyncStorage.removeItem(NOTIFICATION_STATE_KEY);
          const { status } = await Notifications.getPermissionsAsync();
          setNotificationPermission(status === 'granted' ? 'granted' : 'denied');
          const nextState: NotificationState = {};
          const todayStart = toLocalDate(formatDateKey(new Date()));
          for (const item of medications) {
            if (!isValidTime(item.time) || !isValidDate(item.startDate) || !isValidDate(item.endDate)) continue;
            const signature = buildNotificationSignature(item);
            const ids: string[] = [];
            for (const scheduledAt of buildNotificationDates(item, todayStart)) {
              const id = await Notifications.scheduleNotificationAsync({
                content: {
                  title: item.name,
                  body: `${item.dose}${item.note ? ` | ${item.note}` : ''}`,
                  sound: true,
                },
                trigger: {
                  type: Notifications.SchedulableTriggerInputTypes.DATE,
                  date: scheduledAt,
                },
              });
              ids.push(id);
            }
            nextState[item.id] = { signature, ids };
          }
          await AsyncStorage.setItem(NOTIFICATION_STATE_KEY, JSON.stringify(nextState));
          const syncTime = new Date().toISOString();
          setLastNotificationSync(syncTime);
          Alert.alert('Done', 'Notifications resynced successfully.');
        },
      },
    ]);
  }

  function registerForPushToken() {
    if (pushRegistrationStatus === 'registering') return;
    setPushRegistrationStatus('registering');
    Notifications.getExpoPushTokenAsync({
      projectId: 'medication-reminder-mvp',
    }).then((tokenData) => {
      setPushToken(tokenData.data);
      setPushRegistrationStatus('registered');
      saveDevicePushToken(tokenData.data);
    }).catch((error) => {
      console.error('Failed to get push token', error);
      setPushRegistrationStatus('failed');
    });
  }

  function renderMedicationCard(item: MedicationItem) {
    const todayKey = getWeekDayKeyFromDate(new Date(today));
    const isActive = item.endDate >= today && item.repeatDays.includes(todayKey);
    const mealLabel = item.mealTiming === 'before' ? 'Before meal' : item.mealTiming === 'after' ? 'After meal' : '';
    return (
      <View key={item.id} style={[styles.card, isActive ? styles.cardPending : styles.cardTaken]}>
        <View style={styles.cardHeader}>
          <View style={styles.cardMain}>
            <Text style={styles.cardTitle}>{item.name}</Text>
            <Text style={styles.cardMeta}>{item.dose}</Text>
            {item.note ? <Text style={styles.cardNote}>{item.note}</Text> : null}
            {mealLabel ? <Text style={styles.cardMeta}>{mealLabel}</Text> : null}
          </View>
          <View style={styles.badgeContainer}>
            <Text style={styles.badge}>{item.time}</Text>
            {item.isShared && <Text style={styles.badgeShared}>Shared</Text>}
          </View>
        </View>
        <Text style={styles.repeatText}>
          {item.repeatDays.length === 7 ? 'Every day' : item.repeatDays.map((d) => copy.weekdaysShort[d]).join(', ')}
        </Text>
        <Text style={styles.dateText}>{item.startDate} → {item.endDate}</Text>
        <Text style={styles.cardStatus}>{copy.statusPending}</Text>
        <View style={styles.actionRow}>
          <Pressable style={styles.secondaryButton} onPress={() => toggleMedication(item.id)}>
            <Text style={styles.secondaryButtonText}>{copy.actionMarkTaken}</Text>
          </Pressable>
          <Pressable style={styles.secondaryButton} onPress={() => startEditMedication(item.id)}>
            <Text style={styles.secondaryButtonText}>{copy.actionEdit}</Text>
          </Pressable>
          <Pressable style={styles.ghostButton} onPress={() => deleteMedication(item.id)}>
            <Text style={styles.ghostButtonText}>{copy.actionDelete}</Text>
          </Pressable>
        </View>
      </View>
    );
  }

  const today = formatDateKey(new Date());
  const todayKey = getWeekDayKeyFromDate(new Date(today));

  function buildDoseSlots(items: MedicationItem[]): DoseSlot[] {
    const slots: DoseSlot[] = [];
    for (const item of items) {
      if (item.endDate < today) continue;
      if (!item.repeatDays.includes(todayKey)) continue;
      const doseTimes = getDoseTimesForDay(item, new Date(today));
      for (const dt of doseTimes) {
        slots.push({ item, doseTime: dt });
      }
    }
    return slots.sort((a, b) => a.doseTime.localeCompare(b.doseTime));
  }

  const todaySlots = buildDoseSlots(medications);

  function renderDoseSlotCard(slot: DoseSlot) {
    const { item, doseTime } = slot;
    const slotTaken = isSlotTaken(item, today, doseTime);
    const mealLabel = item.mealTiming === 'before' ? 'Before meal' : item.mealTiming === 'after' ? 'After meal' : '';
    return (
      <View key={`${item.id}-${doseTime}`} style={[styles.card, slotTaken ? styles.cardTaken : styles.cardPending]}>
        <View style={styles.cardHeader}>
          <View style={styles.cardMain}>
            <Text style={styles.cardTitle}>{item.name}</Text>
            <Text style={styles.cardMeta}>{item.dose}</Text>
            {item.note ? <Text style={styles.cardNote}>{item.note}</Text> : null}
            {mealLabel ? <Text style={styles.cardMeta}>{mealLabel}</Text> : null}
          </View>
          <View style={styles.badgeContainer}>
            <Text style={styles.badge}>{doseTime}</Text>
            {item.isShared && <Text style={styles.badgeShared}>Shared</Text>}
          </View>
        </View>
        <Text style={styles.cardStatus}>{slotTaken ? copy.statusTaken : copy.statusPending}</Text>
        <View style={styles.actionRow}>
          <Pressable style={styles.secondaryButton} onPress={() => toggleSlot(item.id, today, doseTime)}>
            <Text style={styles.secondaryButtonText}>{slotTaken ? copy.actionMarkPending : copy.actionMarkTaken}</Text>
          </Pressable>
          <Pressable style={styles.secondaryButton} onPress={() => startEditMedication(item.id)}>
            <Text style={styles.secondaryButtonText}>{copy.actionEdit}</Text>
          </Pressable>
          <Pressable style={styles.ghostButton} onPress={() => deleteMedication(item.id)}>
            <Text style={styles.ghostButtonText}>{copy.actionDelete}</Text>
          </Pressable>
        </View>
      </View>
    );
  }

  function renderSection(title: string, items: MedicationItem[], emptyTitle?: string, emptyText?: string) {
    return (
      <View style={styles.section}>
        <View style={styles.sectionHeader}>
          <Text style={styles.sectionTitle}>{title}</Text>
          <Text style={styles.sectionCaption}>{items.length} {copy.listCount}</Text>
        </View>
        {items.length === 0 && emptyTitle && emptyText ? (
          <View style={styles.emptyCard}>
            <Text style={styles.emptyTitle}>{emptyTitle}</Text>
            <Text style={styles.emptyText}>{emptyText}</Text>
          </View>
        ) : (
          items.map(renderMedicationCard)
        )}
      </View>
    );
  }

  function renderTodayScreen() {
    const overdueSlots = todaySlots.filter((s) => !isSlotTaken(s.item, today, s.doseTime) && s.doseTime < formatCurrentTime());
    const upcomingSlots = todaySlots.filter((s) => !isSlotTaken(s.item, today, s.doseTime) && s.doseTime >= formatCurrentTime());
    const laterSlots = upcomingSlots.filter((s) => {
      const [h, m] = s.doseTime.split(':').map(Number);
      const now = new Date();
      const cutoff = new Date(now.getTime() + 2 * 60 * 60 * 1000);
      return h * 60 + m <= cutoff.getHours() * 60 + cutoff.getMinutes();
    });
    const futureSlots = upcomingSlots.filter((s) => {
      const [h, m] = s.doseTime.split(':').map(Number);
      const now = new Date();
      const cutoff = new Date(now.getTime() + 2 * 60 * 60 * 1000);
      return h * 60 + m > cutoff.getHours() * 60 + cutoff.getMinutes();
    });
    const takenSlots = todaySlots.filter((s) => isSlotTaken(s.item, today, s.doseTime));
    const totalSlots = todaySlots.length;
    const takenCount = takenSlots.length;
    const adherence = totalSlots > 0 ? Math.round((takenCount / totalSlots) * 100) : 100;
    const activeTodayCount = medications.filter((m) => m.endDate >= today && m.repeatDays.includes(todayKey)).length;

    return (
      <View style={styles.hero}>
        <Text style={styles.eyebrow}>{copy.heroEyebrow}</Text>
        <Text style={styles.title}>{copy.heroTitle}</Text>
        <Text style={styles.subtitle}>{copy.heroSubtitle}</Text>
        <View style={styles.heroStats}>
          <View style={styles.heroPill}>
            <Text style={styles.heroLabel}>{copy.statsPending}</Text>
            <Text style={styles.heroValue}>{totalSlots - takenCount}</Text>
          </View>
          <View style={styles.heroPill}>
            <Text style={styles.heroLabel}>{copy.statsAdherence}</Text>
            <Text style={styles.heroValue}>{adherence}%</Text>
          </View>
          <View style={styles.heroPill}>
            <Text style={styles.heroLabel}>{copy.statsActiveToday}</Text>
            <Text style={styles.heroValue}>{activeTodayCount}</Text>
          </View>
          <View style={styles.heroPill}>
            <Text style={styles.heroLabel}>{copy.statsTakenToday}</Text>
            <Text style={styles.heroValue}>{takenCount}</Text>
          </View>
        </View>

        {overdueSlots.length > 0 && (
          <View style={styles.sectionOverdue}>
            <Text style={styles.sectionTitleOverdue}>Overdue ({overdueSlots.length})</Text>
            {overdueSlots.map(renderDoseSlotCard)}
          </View>
        )}

        {upcomingSlots.length > 0 && (
          <View style={styles.sectionUpcoming}>
            <Text style={styles.sectionTitleUpcoming}>Upcoming within 2h ({upcomingSlots.length})</Text>
            {laterSlots.map(renderDoseSlotCard)}
          </View>
        )}

        {futureSlots.length > 0 && (
          <View style={styles.section}>
            <Text style={styles.sectionTitle}>Later Today ({futureSlots.length})</Text>
            {futureSlots.map(renderDoseSlotCard)}
          </View>
        )}

        {takenSlots.length > 0 && (
          <View style={styles.section}>
            <Text style={styles.sectionTitle}>{copy.sectionCompleted} ({takenSlots.length})</Text>
            {takenSlots.map(renderDoseSlotCard)}
          </View>
        )}

        {totalSlots === 0 && (
          <View style={styles.emptyCard}>
            <Text style={styles.emptyTitle}>{copy.noTodayItemsTitle}</Text>
            <Text style={styles.emptyText}>{copy.noTodayItemsText}</Text>
          </View>
        )}
      </View>
    );
  }

  function formatCurrentTime() {
    const now = new Date();
    return `${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}`;
  }

  const activeMedications = medications.filter((item) => item.endDate >= today && !areAllSlotsTakenForDay(item, today));
  const expiredMedications = medications.filter((item) => item.endDate < today);

  function renderMedicationsScreen() {
    const runMockOcr = (source: 'camera' | 'gallery') => {
      setOcrLoading(true);
      setTimeout(() => {
        const mockResults = [
          { name: 'Aspirin', dose: '100mg', note: 'Take with food' },
          { name: 'Metformin', dose: '500mg', note: 'After breakfast' },
          { name: 'Vitamin D3', dose: '1000 IU', note: 'Morning' },
        ];
        const result = mockResults[Math.floor(Math.random() * mockResults.length)];
        setOcrResult(result);
        setOcrLoading(false);
      }, 1500);
    };

    const applyOcrResult = () => {
      if (ocrResult) {
        updateDraft('name', ocrResult.name);
        updateDraft('dose', ocrResult.dose);
        updateDraft('note', ocrResult.note);
        setOcrResult(null);
      }
    };

    const sharedMedications = medications.filter((m) => m.isShared);

    return (
      <>
        {renderSection(copy.sectionActive, activeMedications)}
        {renderSection(copy.sectionExpired, expiredMedications)}

        {!showMedForm && (
          <View style={styles.section}>
            <Pressable style={styles.primaryButton} onPress={() => setShowMedForm(true)}>
              <Text style={styles.primaryButtonText}>+ Add Medication</Text>
            </Pressable>
          </View>
        )}

        {sharedMedications.length > 0 && !showMedForm && (
          <View style={styles.section}>
            <View style={styles.sectionHeader}>
              <Text style={styles.sectionTitle}>Home Medicine Cabinet</Text>
              <Text style={styles.sectionCaption}>{sharedMedications.length} shared items</Text>
            </View>
            <Text style={styles.helperText}>Household and shared medicines</Text>
            {sharedMedications.map((item) => renderMedicationCard(item))}
          </View>
        )}

        {showMedForm && (
        <View style={styles.sectionAlt}>
          <Text style={styles.sectionTitle}>{editingId ? copy.editModeTitle : copy.addModeTitle}</Text>
          <Text style={styles.helperText}>{copy.addHelper}</Text>

          <View style={styles.ocrSection}>
            <Text style={styles.ocrLabel}>OCR (Demo)</Text>
            <Text style={styles.ocrHelper}>Scan medication label to auto-fill form</Text>
            {!ocrResult && !ocrLoading && (
              <View style={styles.ocrButtons}>
                <Pressable style={styles.ocrButton} onPress={() => runMockOcr('camera')}>
                  <Text style={styles.ocrButtonText}>Camera</Text>
                </Pressable>
                <Pressable style={styles.ocrButton} onPress={() => runMockOcr('gallery')}>
                  <Text style={styles.ocrButtonText}>Gallery</Text>
                </Pressable>
              </View>
            )}
            {ocrLoading && <Text style={styles.ocrLoading}>Processing OCR... (Demo)</Text>}
            {ocrResult && (
              <View style={styles.ocrResultBox}>
                <Text style={styles.ocrResultTitle}>Detected:</Text>
                <Text style={styles.ocrResultText}>Name: {ocrResult.name}</Text>
                <Text style={styles.ocrResultText}>Dose: {ocrResult.dose}</Text>
                <Text style={styles.ocrResultText}>Note: {ocrResult.note}</Text>
                <View style={styles.ocrResultActions}>
                  <Pressable style={styles.smallButtonPrimary} onPress={applyOcrResult}>
                    <Text style={styles.smallButtonPrimaryText}>Apply to Form</Text>
                  </Pressable>
                  <Pressable style={styles.smallButton} onPress={() => setOcrResult(null)}>
                    <Text style={styles.smallButtonText}>Cancel</Text>
                  </Pressable>
                </View>
              </View>
            )}
          </View>

          <TextInput placeholder={copy.fieldName} placeholderTextColor="#7A867D" style={styles.input} value={draft.name} onChangeText={(value) => updateDraft('name', value)} />
          <TextInput placeholder={copy.fieldDose} placeholderTextColor="#7A867D" style={styles.input} value={draft.dose} onChangeText={(value) => updateDraft('dose', value)} />

          <View style={styles.formGroup}>
            <Text style={styles.fieldLabel}>Time</Text>
            <ScrollView horizontal showsHorizontalScrollIndicator={false} style={styles.pickerRow}>
              {['06:00', '08:00', '10:00', '12:00', '14:00', '18:00', '20:00', '22:00'].map((t) => (
                <Pressable key={t} style={[styles.pickerChip, draft.time === t && styles.pickerChipActive]} onPress={() => updateDraft('time', t)}>
                  <Text style={[styles.pickerChipText, draft.time === t && styles.pickerChipTextActive]}>{t}</Text>
                </Pressable>
              ))}
            </ScrollView>
          </View>

          <View style={styles.formGroup}>
            <Text style={styles.fieldLabel}>Interval</Text>
            <View style={styles.pickerRow}>
              {[12, 24, 48, 72].map((hrs) => (
                <Pressable key={hrs} style={[styles.pickerChip, draft.intervalHours === hrs && styles.pickerChipActive]} onPress={() => updateDraft('intervalHours', hrs)}>
                  <Text style={[styles.pickerChipText, draft.intervalHours === hrs && styles.pickerChipTextActive]}>{hrs}h</Text>
                </Pressable>
              ))}
            </View>
          </View>

          <View style={styles.formGroup}>
            <Text style={styles.fieldLabel}>Meal</Text>
            <View style={styles.pickerRow}>
              <Pressable style={[styles.pickerChip, draft.mealTiming === 'none' && styles.pickerChipActive]} onPress={() => updateDraft('mealTiming', 'none')}>
                <Text style={[styles.pickerChipText, draft.mealTiming === 'none' && styles.pickerChipTextActive]}>Any</Text>
              </Pressable>
              <Pressable style={[styles.pickerChip, draft.mealTiming === 'before' && styles.pickerChipActive]} onPress={() => updateDraft('mealTiming', 'before')}>
                <Text style={[styles.pickerChipText, draft.mealTiming === 'before' && styles.pickerChipTextActive]}>Before</Text>
              </Pressable>
              <Pressable style={[styles.pickerChip, draft.mealTiming === 'after' && styles.pickerChipActive]} onPress={() => updateDraft('mealTiming', 'after')}>
                <Text style={[styles.pickerChipText, draft.mealTiming === 'after' && styles.pickerChipTextActive]}>After</Text>
              </Pressable>
            </View>
          </View>

          <View style={styles.formGroup}>
            <Text style={styles.fieldLabel}>Start Date</Text>
            <ScrollView horizontal showsHorizontalScrollIndicator={false} style={styles.pickerRow}>
              {[0, 1, 2, 3, 4, 5, 6, 7].map((daysFromNow) => {
                const date = new Date();
                date.setDate(date.getDate() + daysFromNow);
                const dateStr = formatDateKey(date);
                const label = `${date.getMonth() + 1}/${date.getDate()}`;
                return (
                  <Pressable key={dateStr} style={[styles.pickerChip, draft.startDate === dateStr && styles.pickerChipActive]} onPress={() => updateDraft('startDate', dateStr)}>
                    <Text style={[styles.pickerChipText, draft.startDate === dateStr && styles.pickerChipTextActive]}>{label}</Text>
                  </Pressable>
                );
              })}
            </ScrollView>
          </View>

          <View style={styles.formGroup}>
            <Text style={styles.fieldLabel}>End Date</Text>
            <ScrollView horizontal showsHorizontalScrollIndicator={false} style={styles.pickerRow}>
              {[7, 14, 30, 60, 90].map((daysFromStart) => {
                if (!draft.startDate) return null;
                const start = toLocalDate(draft.startDate);
                const endDate = addDays(start, daysFromStart);
                const dateStr = formatDateKey(endDate);
                const label = `${endDate.getMonth() + 1}/${endDate.getDate()}`;
                return (
                  <Pressable key={dateStr} style={[styles.pickerChip, draft.endDate === dateStr && styles.pickerChipActive]} onPress={() => updateDraft('endDate', dateStr)}>
                    <Text style={[styles.pickerChipText, draft.endDate === dateStr && styles.pickerChipTextActive]}>{label}</Text>
                  </Pressable>
                );
              })}
            </ScrollView>
          </View>

          <View style={styles.formGroup}>
            <Text style={styles.fieldLabel}>Sharing</Text>
            <View style={styles.pickerRow}>
              <Pressable style={[styles.pickerChip, !draft.isShared && styles.pickerChipActive]} onPress={() => updateDraft('isShared', false)}>
                <Text style={[styles.pickerChipText, !draft.isShared && styles.pickerChipTextActive]}>Private</Text>
              </Pressable>
              <Pressable style={[styles.pickerChip, draft.isShared && styles.pickerChipActive]} onPress={() => updateDraft('isShared', true)}>
                <Text style={[styles.pickerChipText, draft.isShared && styles.pickerChipTextActive]}>Shared</Text>
              </Pressable>
            </View>
          </View>

          {draft.isShared && familyMembers.length > 0 && (
            <View style={styles.formGroup}>
              <Text style={styles.fieldLabel}>Share with</Text>
              <View style={styles.memberSelectGrid}>
                {familyMembers.map((member) => {
                  const isSelected = draft.sharedWithMemberIds.includes(member.id);
                  return (
                    <Pressable key={member.id} style={[styles.memberSelectChip, isSelected && styles.memberSelectChipActive]} onPress={() => {
                      const ids = isSelected
                        ? draft.sharedWithMemberIds.filter((id) => id !== member.id)
                        : [...draft.sharedWithMemberIds, member.id];
                      updateDraft('sharedWithMemberIds', ids);
                    }}>
                      <Text style={[styles.memberSelectChipText, isSelected && styles.memberSelectChipTextActive]}>{member.name}</Text>
                    </Pressable>
                  );
                })}
              </View>
            </View>
          )}

          <TextInput placeholder={copy.fieldNote} placeholderTextColor="#7A867D" style={[styles.input, styles.textArea]} multiline value={draft.note} onChangeText={(value) => updateDraft('note', value)} />
          <Text style={styles.repeatLabel}>{copy.repeatDays}</Text>
          <View style={styles.dayRow}>
            {weekDayKeys.map((day) => {
              const active = draft.repeatDays.includes(day);
              return (
                <Pressable key={day} style={[styles.dayChip, active ? styles.dayChipActive : undefined]} onPress={() => toggleDraftDay(day)}>
                  <Text style={[styles.dayChipText, active ? styles.dayChipTextActive : undefined]}>{copy.weekdaysShort[day]}</Text>
                </Pressable>
              );
            })}
          </View>
          <Pressable style={styles.everyDayButton} onPress={() => updateDraft('repeatDays', [...weekDayKeys])}>
            <Text style={styles.everyDayButtonText}>{copy.everyDay}</Text>
          </Pressable>
          <View style={styles.actionRow}>
            <Pressable style={styles.primaryButton} onPress={submitMedication}>
              <Text style={styles.primaryButtonText}>{editingId ? copy.saveButton : copy.addButton}</Text>
            </Pressable>
            <Pressable style={styles.secondaryButton} onPress={() => { setDraft(createDraft()); setEditingId(null); setShowMedForm(false); }}>
              <Text style={styles.secondaryButtonText}>Cancel</Text>
            </Pressable>
          </View>
        </View>
        )}
      </>
    );
  }

  function renderHistoryScreen() {
    return (
      <View style={styles.section}>
        <View style={styles.sectionHeader}>
          <Text style={styles.sectionTitle}>{copy.sectionHistory}</Text>
          <Text style={styles.sectionCaption}>{historyEntries.length} {copy.listCount}</Text>
        </View>
        {historyEntries.length === 0 ? (
          <View style={styles.emptyCard}>
            <Text style={styles.emptyTitle}>{copy.noHistoryTitle}</Text>
            <Text style={styles.emptyText}>{copy.noHistoryText}</Text>
          </View>
        ) : (
          historyEntries.map((entry) => (
            <View key={entry.id} style={styles.historyRow}>
              <Text style={styles.historyTitle}>{entry.name}</Text>
              <Text style={styles.historyMeta}>{`${entry.date} | ${entry.time} | ${entry.dose}`}</Text>
            </View>
          ))
        )}
      </View>
    );
  }

  const historyEntries = (() => {
    const entries: { id: string; date: string; name: string; dose: string; time: string }[] = [];
    for (const med of medications) {
      for (const slot of med.takenSlots) {
        entries.push({ id: `${med.id}-${slot.date}-${slot.doseTime}`, date: slot.date, name: med.name, dose: med.dose, time: slot.doseTime });
      }
    }
    entries.sort((a, b) => b.date.localeCompare(a.date) || b.time.localeCompare(a.time));
    return entries;
  })();

  function renderFamilyScreen() {
    const handleRemoveMember = (memberId: string) => {
      Alert.alert('Remove Member', 'Are you sure you want to remove this family member?', [
        { text: 'Cancel', style: 'cancel' },
        {
          text: 'Remove',
          style: 'destructive',
          onPress: () => {
            setFamilyMembers(familyMembers.filter((m) => m.id !== memberId));
            setMedicationSharing(medicationSharing
              .map((s) => ({
                ...s,
                sharedWith: s.sharedWith.filter((sw) => sw.memberId !== memberId),
              }))
              .filter((s) => s.isShared === true || s.sharedWith.length > 0));
            setQuickReminders(quickReminders
              .map((r) => ({
                ...r,
                recipientIds: r.recipientIds.filter((id) => id !== memberId),
              }))
              .filter((r) => r.recipientIds.length > 0));
          },
        },
      ]);
    };

    const handleToggleOnlineOffline = (memberId: string) => {
      setFamilyMembers(familyMembers.map((m) =>
        m.id === memberId
          ? { ...m, status: m.status === 'online' ? 'offline' : 'online', lastSeen: new Date().toISOString() }
          : m
      ));
    };

    return (
      <>
        <View style={styles.section}>
          <Text style={styles.sectionTitle}>Family Members</Text>
          {familyMembers.length === 0 ? (
            <View style={styles.emptyCard}>
              <Text style={styles.emptyTitle}>No family members yet</Text>
              <Text style={styles.emptyText}>Add your first family member to start sharing medication reminders</Text>
            </View>
          ) : (
            familyMembers.map((member) => (
              <View key={member.id} style={styles.familyMemberCard}>
                {editingMemberId === member.id ? (
                  <View style={styles.memberEditForm}>
                    <TextInput
                      style={styles.memberEditInput}
                      value={editingMemberName}
                      onChangeText={setEditingMemberName}
                      placeholder="Name"
                      placeholderTextColor="#9E9E9E"
                    />
                    <TextInput
                      style={styles.memberEditInput}
                      value={editingMemberRelation}
                      onChangeText={setEditingMemberRelation}
                      placeholder="Relationship"
                      placeholderTextColor="#9E9E9E"
                    />
                    <Text style={styles.fieldLabel}>Push Token</Text>
                    <TextInput
                      style={styles.memberEditInput}
                      value={editingMemberPushToken}
                      onChangeText={setEditingMemberPushToken}
                      placeholder="ExponentPushToken[xxxxxx]"
                      placeholderTextColor="#9E9E9E"
                      autoCapitalize="none"
                      autoCorrect={false}
                    />
                    {editingMemberPushToken.trim().length > 0 && (
                      isValidExpoPushToken(editingMemberPushToken) ? (
                        <Text style={[styles.helperText, { color: '#2E7D32' }]}>Valid Expo push token format</Text>
                      ) : (
                        <Text style={[styles.helperText, { color: '#C62828' }]}>Invalid token format. Should be ExponentPushToken[xxxxx]</Text>
                      )
                    )}
                    <Text style={styles.helperText}>Ask your family member for their token from Settings &gt; Push Setup in their app.</Text>
                    <View style={styles.memberEditActions}>
                      <Pressable style={styles.smallButton} onPress={() => setEditingMemberId(null)}>
                        <Text style={styles.smallButtonText}>Cancel</Text>
                      </Pressable>
                      <Pressable style={styles.smallButtonPrimary} onPress={() => {
                        const updatedName = editingMemberName.trim() || member.name;
                        const rawToken = editingMemberPushToken.trim();
                        const updatedToken = rawToken.length > 0 && !isValidExpoPushToken(rawToken) ? undefined : (rawToken || undefined);
                        if (rawToken.length > 0 && !isValidExpoPushToken(rawToken)) {
                          Alert.alert('Invalid Token', 'The push token format is incorrect. Please check and try again, or leave it empty.');
                          return;
                        }
                        setFamilyMembers(familyMembers.map((m) =>
                          m.id === member.id
                            ? { ...m, name: updatedName, relationship: editingMemberRelation.trim() || m.relationship, pushToken: updatedToken }
                            : m
                        ));
                        saveFamilyMemberPushToken(member.id, updatedName, updatedToken);
                        setEditingMemberId(null);
                      }}>
                        <Text style={styles.smallButtonPrimaryText}>Save</Text>
                      </Pressable>
                    </View>
                  </View>
                ) : (
                  <>
                    <View style={styles.familyMemberInfo}>
                      <Text style={styles.familyMemberName}>{member.name}</Text>
                      <Text style={styles.familyMemberRelation}>{member.relationship}</Text>
                      {member.pushToken ? (
                        isValidExpoPushToken(member.pushToken) ? (
                          <View style={styles.tokenStatusRow}>
                            <View style={styles.tokenStatusDot} />
                            <Text style={[styles.familyMemberRelation, { color: '#2E7D32' }]}>Push token ready</Text>
                          </View>
                        ) : (
                          <View style={styles.tokenStatusRow}>
                            <View style={[styles.tokenStatusDot, { backgroundColor: '#C62828' }]} />
                            <Text style={[styles.familyMemberRelation, { color: '#C62828' }]}>Token invalid (format issue)</Text>
                          </View>
                        )
                      ) : (
                        <View style={styles.tokenStatusRow}>
                          <View style={[styles.tokenStatusDot, { backgroundColor: '#9E9E9E' }]} />
                          <Text style={[styles.familyMemberRelation, { color: '#9E9E9E', fontStyle: 'italic' }]}>No push token</Text>
                        </View>
                      )}
                    </View>
                    <View style={styles.familyMemberActions}>
                      <Pressable style={styles.statusToggle} onPress={() => handleToggleOnlineOffline(member.id)}>
                        <View style={[styles.statusDot, member.status === 'online' ? styles.statusDotOnline : styles.statusDotOffline]} />
                        <Text style={styles.statusToggleText}>{member.status === 'online' ? 'Online' : 'Offline'}</Text>
                      </Pressable>
                      <View style={styles.memberActionButtons}>
                        <Pressable style={styles.iconButton} onPress={() => {
                          setEditingMemberId(member.id);
                          setEditingMemberName(member.name);
                          setEditingMemberRelation(member.relationship);
                          setEditingMemberPushToken(member.pushToken || '');
                        }}>
                          <Text style={styles.iconButtonText}>Edit</Text>
                        </Pressable>
                        <Pressable style={styles.iconButtonDanger} onPress={() => handleRemoveMember(member.id)}>
                          <Text style={styles.iconButtonDangerText}>Remove</Text>
                        </Pressable>
                      </View>
                    </View>
                  </>
                )}
              </View>
            ))
          )}
          <Pressable style={styles.secondaryButton} onPress={() => {
            const newMember: FamilyMember = {
              id: `member-${Date.now()}`,
              name: `Family Member ${familyMembers.length + 1}`,
              relationship: 'Relative',
              status: 'offline',
              lastSeen: new Date().toISOString(),
            };
            setFamilyMembers([...familyMembers, newMember]);
          }}>
            <Text style={styles.secondaryButtonText}>Add Family Member (Demo)</Text>
          </Pressable>
        </View>

        <View style={styles.section}>
          <Text style={styles.sectionTitle}>Medication Sharing</Text>
          {medications.length === 0 ? (
            <Text style={styles.helperText}>No medications to share</Text>
          ) : (
            medications.slice(0, 5).map((med) => {
              const sharing = medicationSharing.find((s) => s.medicationId === med.id);
              const sharedCount = sharing?.sharedWith.length || 0;
              const isShared = sharing?.isShared || false;
              return (
                <View key={med.id} style={styles.sharingMedCard}>
                  <View style={styles.sharingMedInfo}>
                    <Text style={styles.sharingMedName}>{med.name}</Text>
                    <Text style={styles.sharingMedDose}>{med.dose}</Text>
                  </View>
                  <View style={styles.sharingMedActions}>
                    <Pressable
                      style={[styles.toggleButton, isShared ? styles.toggleButtonActive : styles.toggleButtonInactive]}
                      onPress={() => {
                        if (familyMembers.length === 0) {
                          Alert.alert('No Family Members', 'Add family members first to share medications.');
                          return;
                        }
                        if (isShared) {
                          setMedicationSharing(medicationSharing.filter((s) => s.medicationId !== med.id));
                        } else {
                          const newSharing: MedicationShare = {
                            medicationId: med.id,
                            isShared: true,
                            sharedWith: [],
                          };
                          setMedicationSharing([...medicationSharing, newSharing]);
                        }
                      }}
                    >
                      <Text style={[styles.toggleButtonText, isShared ? styles.toggleButtonTextActive : styles.toggleButtonTextInactive]}>
                        {isShared ? 'Shared' : 'Private'}
                      </Text>
                    </Pressable>
                  </View>
                  {isShared && (
                    <>
                      <Text style={styles.sharingSectionLabel}>Select members to share with:</Text>
                      {familyMembers.map((member) => {
                        const share = sharing?.sharedWith.find((s) => s.memberId === member.id);
                        const isSelected = !!share;
                        return (
                          <View key={member.id} style={styles.memberSelectRow}>
                            <Pressable
                              style={styles.checkbox}
                              onPress={() => {
                                const updatedSharing = medicationSharing.find((s) => s.medicationId === med.id);
                                if (!updatedSharing) return;
                                let newSharedWith: MedicationShare['sharedWith'];
                                if (isSelected) {
                                  newSharedWith = updatedSharing.sharedWith.filter((s) => s.memberId !== member.id);
                                } else {
                                  newSharedWith = [...updatedSharing.sharedWith, { memberId: member.id, permission: 'view' }];
                                }
                                setMedicationSharing(medicationSharing.map((s) =>
                                  s.medicationId === med.id ? { ...s, sharedWith: newSharedWith } : s
                                ));
                              }}
                            >
                              <View style={[styles.checkboxInner, isSelected ? styles.checkboxChecked : styles.checkboxUnchecked]}>
                                {isSelected && <Text style={styles.checkmark}>✓</Text>}
                              </View>
                            </Pressable>
                            <Text style={styles.memberSelectName}>{member.name}</Text>
                            {isSelected && share && (
                              <Pressable onPress={() => {
                                const permissions: PermissionLevel[] = ['view', 'edit', 'admin'];
                                const currentIndex = permissions.indexOf(share.permission);
                                const nextPermission = permissions[(currentIndex + 1) % permissions.length];
                                setMedicationSharing(medicationSharing.map((s) =>
                                  s.medicationId === med.id
                                    ? { ...s, sharedWith: s.sharedWith.map((m) => m.memberId === member.id ? { ...m, permission: nextPermission } : m) }
                                    : s
                                ));
                              }}>
                                <Text style={styles.permissionBadge}>{share.permission}</Text>
                              </Pressable>
                            )}
                          </View>
                        );
                      })}
                      {sharedCount > 0 && (
                        <Text style={styles.sharingSummary}>Shared with {sharedCount} member(s)</Text>
                      )}
                    </>
                  )}
                </View>
              );
            })
          )}
        </View>

        <View style={styles.section}>
          <Text style={styles.sectionTitle}>Quick Reminder</Text>
          {!reminderForm ? (
            <>
              <Text style={styles.helperText}>Send medication reminders to selected family members</Text>
              <Pressable style={styles.primaryButton} onPress={() => {
                if (familyMembers.length === 0 || medications.length === 0) {
                  Alert.alert('Missing Data', 'Add family members and medications first.');
                  return;
                }
                setReminderForm({ medicationId: medications[0].id, recipientIds: [] });
              }}>
                <Text style={styles.primaryButtonText}>Create Reminder</Text>
              </Pressable>
            </>
          ) : (
            <>
              <Text style={styles.formLabel}>Select Medication:</Text>
              {medications.slice(0, 5).map((med) => (
                <Pressable
                  key={med.id}
                  style={[styles.radioOption, reminderForm.medicationId === med.id && styles.radioOuterSelected]}
                  onPress={() => setReminderForm({ ...reminderForm, medicationId: med.id })}
                >
                  <View style={[styles.radioOuter, reminderForm.medicationId === med.id && styles.radioOuterSelected]}>
                    {reminderForm.medicationId === med.id && <View style={styles.radioInner} />}
                  </View>
                  <Text style={[styles.radioLabel, reminderForm.medicationId === med.id && styles.radioLabelSelected]}>
                    {med.name} ({med.dose})
                  </Text>
                </Pressable>
              ))}
              <Text style={styles.formLabel}>Select Recipients:</Text>
              {familyMembers.map((member) => {
                const isSelected = reminderForm.recipientIds.includes(member.id);
                const hasToken = !!member.pushToken;
                const tokenValid = hasToken && isValidExpoPushToken(member.pushToken!);
                return (
                  <Pressable
                    key={member.id}
                    style={styles.checkboxRow}
                    onPress={() => {
                      const newRecipients = isSelected
                        ? reminderForm.recipientIds.filter((id) => id !== member.id)
                        : [...reminderForm.recipientIds, member.id];
                      setReminderForm({ ...reminderForm, recipientIds: newRecipients });
                    }}
                  >
                    <View style={[styles.checkboxInner, isSelected ? styles.checkboxChecked : styles.checkboxUnchecked]}>
                      {isSelected && <Text style={styles.checkmark}>✓</Text>}
                    </View>
                    <View style={styles.recipientInfo}>
                      <Text style={styles.checkboxLabel}>{member.name}</Text>
                      {!hasToken && <Text style={styles.recipientTokenStatus}>No token — demo only</Text>}
                      {hasToken && !tokenValid && <Text style={[styles.recipientTokenStatus, { color: '#C62828' }]}>Invalid token — will fail</Text>}
                      {hasToken && tokenValid && <Text style={[styles.recipientTokenStatus, { color: '#2E7D32' }]}>Token ready</Text>}
                    </View>
                  </Pressable>
                );
              })}
              <View style={styles.formActions}>
                <Pressable style={styles.secondaryButton} onPress={() => setReminderForm(null)}>
                  <Text style={styles.secondaryButtonText}>Cancel</Text>
                </Pressable>
                <Pressable
                  style={[styles.primaryButton, reminderForm.recipientIds.length === 0 && styles.buttonDisabled]}
                  disabled={reminderForm.recipientIds.length === 0}
                  onPress={() => {
                    const med = medications.find((m) => m.id === reminderForm.medicationId);
                    if (!med) return;
                    const newReminder: QuickReminder = {
                      id: `reminder-${Date.now()}`,
                      medicationName: med.name,
                      medicationDose: med.dose,
                      recipientIds: reminderForm.recipientIds,
                      sentAt: new Date().toISOString(),
                      status: 'sent',
                    };
                    setQuickReminders([...quickReminders, newReminder]);
                    setReminderForm(null);
                    Alert.alert('Reminder Sent', `Sent to ${reminderForm.recipientIds.length} recipient(s) (Demo)`);
                  }}
                >
                  <Text style={styles.primaryButtonText}>Send ({reminderForm.recipientIds.length})</Text>
                </Pressable>
              </View>
              {firebaseReady && reminderForm.recipientIds.length > 0 && (() => {
                const allTokens = reminderForm.recipientIds
                  .map(id => ({ id, token: familyMembers.find(fm => fm.id === id)?.pushToken }))
                  .filter((t): t is { id: string; token: string } => !!t.token);
                const validTokens = allTokens.filter(t => isValidExpoPushToken(t.token));
                const invalidCount = allTokens.length - validTokens.length;
                if (validTokens.length === 0) {
                  return (
                    <Text style={[styles.helperText, { color: '#C62828' }]}>
                      No recipients have valid Expo push tokens. Add valid tokens in member Edit screen to enable real push.
                    </Text>
                  );
                }
                return (
                  <>
                    {invalidCount > 0 && (
                      <Text style={[styles.helperText, { color: '#F57C00' }]}>
                        {invalidCount} selected member(s) have invalid tokens and will not receive push.
                      </Text>
                    )}
                    <Pressable style={styles.primaryButton} onPress={async () => {
                      const med = medications.find((m) => m.id === reminderForm.medicationId);
                      if (!med) return;
                      const result = await sendRealPushNotification(
                        validTokens.map(t => t.token),
                        med.name,
                        `Time to take ${med.dose}${med.note ? `: ${med.note}` : ''}`,
                        validTokens.map(t => t.id)
                      );
                      if (result.success) {
                        Alert.alert('Push Sent', `Delivered to ${result.sent} of ${validTokens.length} recipient(s).`);
                      } else {
                        Alert.alert('Push Failed', result.error || 'Unknown error');
                      }
                    }}>
                      <Text style={styles.primaryButtonText}>Send via Worker ({validTokens.length} ready)</Text>
                    </Pressable>
                  </>
                );
              })()}
            </>
          )}
        </View>

        <View style={styles.section}>
          <Text style={styles.sectionTitle}>Recent Reminders</Text>
          {quickReminders.length === 0 ? (
            <Text style={styles.helperText}>No recent reminders</Text>
          ) : (
            quickReminders.slice(-5).reverse().map((reminder) => (
              <View key={reminder.id} style={styles.reminderCard}>
                <View style={styles.reminderHeader}>
                  <Text style={styles.reminderMed}>{reminder.medicationName} ({reminder.medicationDose})</Text>
                  <View style={[styles.statusBadge, reminder.status === 'sent' ? styles.statusBadgeSent : reminder.status === 'delivered' ? styles.statusBadgeDelivered : styles.statusBadgeFailed]}>
                    <Text style={styles.statusBadgeText}>{reminder.status}</Text>
                  </View>
                </View>
                <Text style={styles.reminderRecipients}>To: {reminder.recipientIds.map((id) => familyMembers.find((m) => m.id === id)?.name || 'Unknown').join(', ')}</Text>
                <Text style={styles.reminderTime}>{new Date(reminder.sentAt).toLocaleString()}</Text>
                <View style={styles.reminderActions}>
                  <Pressable style={styles.smallButton} onPress={() => {
                    setQuickReminders(quickReminders.map((r) => r.id === reminder.id ? { ...r, status: 'delivered' as const } : r));
                  }}>
                    <Text style={styles.smallButtonText}>Mark Delivered</Text>
                  </Pressable>
                  <Pressable style={styles.smallButton} onPress={() => {
                    setQuickReminders(quickReminders.map((r) => r.id === reminder.id ? { ...r, status: 'failed' as const } : r));
                  }}>
                    <Text style={styles.smallButtonText}>Mark Failed</Text>
                  </Pressable>
                </View>
              </View>
            ))
          )}
        </View>

        <View style={styles.section}>
          <Text style={styles.sectionTitle}>Received Reminders (Demo)</Text>
          <Text style={styles.helperText}>Preview how incoming reminders from family members would appear</Text>
          <Pressable style={styles.secondaryButton} onPress={() => {
            if (familyMembers.length === 0 || medications.length === 0) {
              Alert.alert('Missing Data', 'Add family members and medications first.');
              return;
            }
            const fromMember = familyMembers[Math.floor(Math.random() * familyMembers.length)];
            const med = medications[Math.floor(Math.random() * medications.length)];
            const newReceived = {
              id: `received-${Date.now()}`,
              fromName: fromMember.name,
              medicationName: med.name,
              medicationDose: med.dose,
              receivedAt: new Date().toISOString(),
              status: 'new' as const,
            };
            setReceivedReminders([...receivedReminders, newReceived]);
            Alert.alert('Demo', `Simulated receiving a reminder from ${fromMember.name}`);
          }}>
            <Text style={styles.secondaryButtonText}>Simulate Received Reminder</Text>
          </Pressable>
          {receivedReminders.length === 0 ? (
            <Text style={styles.helperText}>No received reminders yet</Text>
          ) : (
            receivedReminders.slice(-5).reverse().map((received) => (
              <View key={received.id} style={[styles.reminderCard, received.status === 'new' && styles.reminderCardNew]}>
                <View style={styles.reminderHeader}>
                  <Text style={styles.reminderMed}>{received.medicationName} ({received.medicationDose})</Text>
                  <View style={[styles.statusBadge, received.status === 'new' ? styles.statusBadgeSent : styles.statusBadgeDelivered]}>
                    <Text style={styles.statusBadgeText}>{received.status === 'new' ? 'New' : 'Seen'}</Text>
                  </View>
                </View>
                <Text style={styles.reminderRecipients}>From: {received.fromName}</Text>
                <Text style={styles.reminderTime}>{new Date(received.receivedAt).toLocaleString()}</Text>
                <View style={styles.reminderActions}>
                  <Pressable style={styles.smallButtonPrimary} onPress={() => {
                    setReceivedReminders(receivedReminders.map((r) => r.id === received.id ? { ...r, status: 'acknowledged' as const } : r));
                  }}>
                    <Text style={styles.smallButtonPrimaryText}>Acknowledge</Text>
                  </Pressable>
                </View>
              </View>
            ))
          )}
        </View>

        <View style={styles.section}>
          <Text style={styles.sectionTitle}>WiFi Direct (Demo)</Text>
          <Text style={styles.helperText}>This is a local-only demo. Real WiFi Direct requires native module integration.</Text>
          <View style={styles.settingRow}>
            <Text style={styles.settingLabel}>Status</Text>
            <Text style={[styles.settingValue, { color: wifiState === 'connected' ? '#2E7D32' : wifiState === 'scanning' ? '#F57C00' : '#5B6C67' }]}>
              {wifiState === 'idle' ? 'Idle' : wifiState === 'scanning' ? 'Scanning...' : 'Connected'}
            </Text>
          </View>
          {connectedDevice && (
            <View style={styles.settingRow}>
              <Text style={styles.settingLabel}>Connected to</Text>
              <Text style={styles.settingValue}>{connectedDevice.name}</Text>
            </View>
          )}
          {wifiState !== 'scanning' && (
            <Pressable style={styles.secondaryButton} onPress={() => {
              setWifiState('scanning');
              setWifiDevices([]);
              setTimeout(() => {
                const mockDevices = [
                  { id: 'device-1', name: 'Mom\'s Phone' },
                  { id: 'device-2', name: 'Dad\'s Tablet' },
                ];
                setWifiDevices(mockDevices);
                setWifiState('idle');
              }, 2000);
            }}>
              <Text style={styles.secondaryButtonText}>Scan for Devices</Text>
            </Pressable>
          )}
          {wifiState === 'scanning' && (
            <Text style={styles.helperText}>Searching for nearby devices...</Text>
          )}
          {wifiDevices.length > 0 && wifiState === 'idle' && (
            <View style={styles.wifiDeviceList}>
              <Text style={styles.formLabel}>Available Devices:</Text>
              {wifiDevices.map((device) => (
                <View key={device.id} style={styles.wifiDeviceRow}>
                  <Text style={styles.wifiDeviceName}>{device.name}</Text>
                  <Pressable style={styles.smallButton} onPress={() => {
                    setConnectedDevice(device);
                    setWifiState('connected');
                    setWifiDevices([]);
                    Alert.alert('Connected', `Connected to ${device.name} (Demo)`);
                  }}>
                    <Text style={styles.smallButtonText}>Connect</Text>
                  </Pressable>
                </View>
              ))}
            </View>
          )}
          {wifiState === 'connected' && (
            <Pressable style={styles.ghostButton} onPress={() => {
              Alert.alert('Disconnect', `Disconnect from ${connectedDevice?.name}?`, [
                { text: 'Cancel', style: 'cancel' },
                {
                  text: 'Disconnect',
                  style: 'destructive',
                  onPress: () => {
                    setConnectedDevice(null);
                    setWifiState('idle');
                  },
                },
              ]);
            }}>
              <Text style={styles.ghostButtonText}>Disconnect</Text>
            </Pressable>
          )}
        </View>
      </>
    );
  }

  function renderSettingsScreen() {
    return (
      <>
        <View style={styles.section}>
          <Text style={styles.sectionTitle}>Notifications</Text>
          <View style={styles.settingRow}>
            <Text style={styles.settingLabel}>Permission Status</Text>
            <Text style={[styles.settingValue, { color: notificationPermission === 'granted' ? '#2E7D32' : notificationPermission === 'denied' ? '#C62828' : '#F57C00' }]}>
              {notificationPermission === 'granted' ? 'Granted' : notificationPermission === 'denied' ? 'Denied' : 'Unknown'}
            </Text>
          </View>
          {notificationPermission !== 'granted' && (
            <Pressable style={styles.secondaryButton} onPress={async () => {
              const { status } = await Notifications.requestPermissionsAsync();
              setNotificationPermission(status === 'granted' ? 'granted' : 'denied');
              if (status === 'granted') {
                Alert.alert('Permissions', 'Notification permissions granted.');
              }
            }}>
              <Text style={styles.secondaryButtonText}>Request Permissions</Text>
            </Pressable>
          )}
          <Pressable style={styles.secondaryButton} onPress={resyncNotifications}>
            <Text style={styles.secondaryButtonText}>Resync Notifications</Text>
          </Pressable>
          {lastNotificationSync && (
            <View style={styles.settingRow}>
              <Text style={styles.settingLabel}>Last Sync</Text>
              <Text style={styles.settingValue}>{new Date(lastNotificationSync).toLocaleString()}</Text>
            </View>
          )}
        </View>
        <View style={styles.section}>
          <Text style={styles.sectionTitle}>Push Setup</Text>
          <View style={styles.settingRow}>
            <Text style={styles.settingLabel}>Notification Permission</Text>
            <Text style={[styles.settingValue, { color: notificationPermission === 'granted' ? '#2E7D32' : '#C62828' }]}>
              {notificationPermission === 'granted' ? 'Granted' : 'Denied'}
            </Text>
          </View>
          <View style={styles.settingRow}>
            <Text style={styles.settingLabel}>Expo Push Token</Text>
            <Text style={[styles.settingValue, { color: pushRegistrationStatus === 'registered' ? '#2E7D32' : '#5B6C67' }]}>
              {pushRegistrationStatus === 'idle' ? 'Not registered' : pushRegistrationStatus === 'registering' ? 'Registering...' : pushRegistrationStatus === 'registered' ? 'Registered' : 'Failed'}
            </Text>
          </View>
          <View style={styles.settingRow}>
            <Text style={styles.settingLabel}>Push Worker</Text>
            <Text style={[styles.settingValue, { color: firebaseReady ? '#2E7D32' : '#F57C00' }]}>
              {firebaseReady ? 'Ready' : 'Not configured'}
            </Text>
          </View>
          {pushToken ? (
            <View style={styles.settingRow}>
              <Text style={styles.settingLabel}>Token</Text>
              <Text style={[styles.settingValue, { fontSize: 10, maxWidth: 200 }]} numberOfLines={1}>{pushToken.slice(0, 32)}...</Text>
            </View>
          ) : null}
          <View style={styles.settingRow}>
            <Text style={styles.settingLabel}>Ready for Full Push</Text>
            <Text style={[styles.settingValue, { color: notificationPermission === 'granted' && pushRegistrationStatus === 'registered' && firebaseReady ? '#2E7D32' : '#F57C00' }]}>
              {notificationPermission === 'granted' && pushRegistrationStatus === 'registered' && firebaseReady ? 'Yes' : 'Not yet'}
            </Text>
          </View>
          {notificationPermission === 'granted' && (
            <Pressable style={styles.secondaryButton} onPress={registerForPushToken}>
              <Text style={styles.secondaryButtonText}>
                {pushRegistrationStatus === 'registering' ? 'Registering...' : pushRegistrationStatus === 'registered' ? 'Re-register Token' : 'Register Push Token'}
              </Text>
            </Pressable>
          )}
          {!firebaseReady ? (
            <Text style={styles.helperText}>Push backend not configured. Token writes are local only.</Text>
          ) : (
            <Text style={styles.helperText}>Worker ready. Deploy worker/ and set PUSH_WORKER_URL to enable real push.</Text>
          )}
        </View>
        <View style={styles.section}>
          <Text style={styles.sectionTitle}>Token Sharing</Text>
          <Text style={styles.helperText}>Your push token is how family members send you reminders. Share it with them so they can add you to their app.</Text>
          {pushToken ? (
            <>
              <View style={styles.settingRow}>
                <Text style={styles.settingLabel}>Your Token</Text>
                <Text style={[styles.settingValue, { fontSize: 10, maxWidth: 200 }]} numberOfLines={1}>{pushToken}</Text>
              </View>
              <Text style={[styles.helperText, { fontStyle: 'italic' }]}>
                Give this token to family members. They add it in Family &gt; Edit Member &gt; Push Token.
              </Text>
            </>
          ) : (
            <Text style={styles.helperText}>Register a push token above to get your token to share.</Text>
          )}
          <Text style={styles.helperText}>Token format: ExponentPushToken[xxxxx] — looks like a long random string.</Text>
        </View>
        <View style={styles.section}>
          <Text style={styles.sectionTitle}>Language</Text>
          <View style={styles.settingRow}>
            <Text style={styles.settingLabel}>Current Language</Text>
            <Text style={styles.settingValue}>English</Text>
          </View>
        </View>
        <View style={styles.section}>
          <Text style={styles.sectionTitle}>About</Text>
          <View style={styles.settingRow}>
            <Text style={styles.settingLabel}>Version</Text>
            <Text style={styles.settingValue}>1.0.0</Text>
          </View>
          <View style={styles.settingRow}>
            <Text style={styles.settingLabel}>Build</Text>
            <Text style={styles.settingValue}>2026.04.11</Text>
          </View>
        </View>
        <View style={styles.section}>
          <Text style={styles.sectionTitle}>Medical Disclaimer</Text>
          <Text style={styles.disclaimerText}>
            This app is a medication reminder tool for personal use. It does not provide medical advice, diagnosis, or treatment. Always consult your healthcare provider about medications and health decisions.
          </Text>
          <Text style={styles.disclaimerText}>
            Reminders are for reference only. This app does not guarantee timely medication intake. Users are responsible for ensuring medications are taken safely and correctly.
          </Text>
        </View>
        <View style={styles.section}>
          <Text style={styles.sectionTitle}>App Diagnostics</Text>
          <View style={styles.diagnosticsGrid}>
            <View style={styles.diagItem}>
              <Text style={styles.diagValue}>{todayMedications.length}</Text>
              <Text style={styles.diagLabel}>Today</Text>
            </View>
            <View style={styles.diagItem}>
              <Text style={styles.diagValue}>{medications.length}</Text>
              <Text style={styles.diagLabel}>Total</Text>
            </View>
            <View style={styles.diagItem}>
              <Text style={styles.diagValue}>{historyEntries.length}</Text>
              <Text style={styles.diagLabel}>History</Text>
            </View>
            <View style={styles.diagItem}>
              <Text style={styles.diagValue}>{familyMembers.length}</Text>
              <Text style={styles.diagLabel}>Family</Text>
            </View>
          </View>
        </View>
        <View style={styles.section}>
          <Text style={styles.sectionTitle}>{copy.sectionRoadmap}</Text>
          <Text style={styles.bullet}>• {copy.roadmap1}</Text>
          <Text style={styles.bullet}>• {copy.roadmap2}</Text>
          <Text style={styles.bullet}>• {copy.roadmap3}</Text>
          <Text style={styles.bullet}>• {copy.roadmap4}</Text>
        </View>
        <View style={styles.section}>
          <Text style={styles.sectionTitle}>{copy.sectionFamily}</Text>
          {familyUpdates.map((item) => (
            <View key={item} style={styles.listRow}>
              <Text style={styles.listDot}>•</Text>
              <Text style={styles.listText}>{item}</Text>
            </View>
          ))}
        </View>
      </>
    );
  }

  const todayMedications = medications.filter((item) => item.endDate >= today && item.repeatDays.includes(todayKey));

  function renderCurrentScreen() {
    if (screen === 'medications') return renderMedicationsScreen();
    if (screen === 'history') return renderHistoryScreen();
    if (screen === 'family') return renderFamilyScreen();
    if (screen === 'settings') return renderSettingsScreen();
    return renderTodayScreen();
  }

  const tabs = [
    { key: 'today' as const, label: copy.tabToday },
    { key: 'medications' as const, label: copy.tabMedications },
    { key: 'history' as const, label: copy.tabHistory },
    { key: 'family' as const, label: 'Family' },
    { key: 'settings' as const, label: copy.tabSettings },
  ];

  return (
    <SafeAreaProvider>
      <SafeAreaView style={styles.safeArea}>
        <StatusBar style="dark" />
        <View style={styles.appShell}>
          <ScrollView contentContainerStyle={[styles.content, { paddingBottom: 118 }]}>{renderCurrentScreen()}</ScrollView>
          <View style={[styles.tabBar, { bottom: 18 }]}>
            {tabs.map((tab) => (
              <Pressable
                key={tab.key}
                style={[styles.tabButton, screen === tab.key ? styles.tabButtonActive : undefined]}
                onPress={() => setScreen(tab.key)}
              >
                <Text style={[styles.tabButtonText, screen === tab.key ? styles.tabButtonTextActive : undefined]}>
                  {tab.label}
                </Text>
              </Pressable>
            ))}
          </View>
        </View>
      </SafeAreaView>
    </SafeAreaProvider>
  );
}

const styles = StyleSheet.create({
  safeArea: { flex: 1, backgroundColor: '#F5F1E8' },
  appShell: { flex: 1 },
  content: { padding: 20, gap: 18, paddingBottom: 110 },
  hero: { backgroundColor: '#183A37', borderRadius: 28, padding: 24, gap: 12 },
  eyebrow: { color: '#A7D8CE', fontSize: 12, fontWeight: '800', letterSpacing: 1, textTransform: 'uppercase' },
  title: { color: '#F8F3EA', fontSize: 34, fontWeight: '800' },
  subtitle: { color: '#D9E6E2', fontSize: 15, lineHeight: 22 },
  heroStats: { flexDirection: 'row', flexWrap: 'wrap', gap: 12 },
  heroPill: { minWidth: '47%', flex: 1, borderRadius: 20, backgroundColor: '#234A46', padding: 14, gap: 4 },
  heroLabel: { color: '#BFDCD5', fontSize: 12, fontWeight: '700' },
  heroValue: { color: '#F8F3EA', fontSize: 24, fontWeight: '800' },
  section: { backgroundColor: '#FFFDF8', borderRadius: 24, padding: 18, gap: 12, borderWidth: 1, borderColor: '#E1D7C8' },
  sectionAlt: { backgroundColor: '#E7F2ED', borderRadius: 24, padding: 18, gap: 12, borderWidth: 1, borderColor: '#CFE0D8' },
  sectionHeader: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center' },
  sectionTitle: { color: '#183A37', fontSize: 20, fontWeight: '800' },
  sectionCaption: { color: '#4A5B56', fontSize: 13, fontWeight: '700' },
  helperText: { color: '#3E5B54', fontSize: 14, lineHeight: 20 },
  input: { backgroundColor: '#F8FCF9', borderRadius: 16, paddingHorizontal: 14, paddingVertical: 14, fontSize: 15, color: '#1F3430', borderWidth: 1, borderColor: '#C8DBD2' },
  textArea: { minHeight: 88, textAlignVertical: 'top' },
  repeatLabel: { color: '#183A37', fontSize: 14, fontWeight: '700' },
  dayRow: { flexDirection: 'row', flexWrap: 'wrap', gap: 8 },
  dayChip: { paddingHorizontal: 12, paddingVertical: 10, borderRadius: 999, backgroundColor: '#F8FCF9', borderWidth: 1, borderColor: '#C8DBD2' },
  dayChipActive: { backgroundColor: '#183A37', borderColor: '#183A37' },
  dayChipText: { color: '#284A43', fontSize: 13, fontWeight: '700' },
  dayChipTextActive: { color: '#FFFFFF' },
  everyDayButton: { alignSelf: 'flex-start', paddingHorizontal: 14, paddingVertical: 10, borderRadius: 999, backgroundColor: '#DDEDE6' },
  everyDayButtonText: { color: '#284A43', fontSize: 13, fontWeight: '700' },
  card: { borderRadius: 20, padding: 16, gap: 10, borderWidth: 1 },
  cardPending: { backgroundColor: '#F8F4EC', borderColor: '#E5DACB' },
  cardTaken: { backgroundColor: '#EEF5F1', borderColor: '#CFE0D8' },
  cardHeader: { flexDirection: 'row', justifyContent: 'space-between', gap: 12, alignItems: 'flex-start' },
  cardMain: { flex: 1, gap: 4 },
  cardTitle: { color: '#233633', fontSize: 18, fontWeight: '800' },
  badge: { color: '#183A37', backgroundColor: '#D4E7DE', paddingHorizontal: 12, paddingVertical: 6, borderRadius: 999, overflow: 'hidden', fontWeight: '800' },
  badgeContainer: { alignItems: 'flex-end', gap: 4 },
  badgeShared: { color: '#FFFFFF', backgroundColor: '#2E7D32', paddingHorizontal: 8, paddingVertical: 4, borderRadius: 999, overflow: 'hidden', fontSize: 10, fontWeight: '700' },
  cardMeta: { color: '#5B6C67', fontSize: 14, fontWeight: '600' },
  cardNote: { color: '#667670', fontSize: 14, lineHeight: 20 },
  repeatText: { color: '#6A756F', fontSize: 13, fontWeight: '600' },
  dateText: { color: '#6A756F', fontSize: 13 },
  cardStatus: { color: '#2E5E52', fontSize: 14, fontWeight: '700' },
  actionRow: { flexDirection: 'row', flexWrap: 'wrap', gap: 10 },
  primaryButton: { backgroundColor: '#183A37', borderRadius: 16, paddingHorizontal: 16, paddingVertical: 12 },
  primaryButtonText: { color: '#FFFFFF', fontSize: 14, fontWeight: '700' },
  secondaryButton: { backgroundColor: '#EDF5F1', borderRadius: 16, paddingHorizontal: 16, paddingVertical: 12 },
  secondaryButtonText: { color: '#284A43', fontSize: 14, fontWeight: '700' },
  ghostButton: { borderRadius: 16, paddingHorizontal: 16, paddingVertical: 12, borderWidth: 1, borderColor: '#D3D8D4', backgroundColor: '#FFFDF8' },
  ghostButtonText: { color: '#5C6662', fontSize: 14, fontWeight: '700' },
  emptyCard: { backgroundColor: '#FBF7EF', borderRadius: 20, padding: 18, gap: 8, borderWidth: 1, borderColor: '#E4D8C6' },
  emptyTitle: { color: '#223632', fontSize: 18, fontWeight: '800' },
  emptyText: { color: '#69756E', fontSize: 14, lineHeight: 20 },
  historyRow: { gap: 4, paddingVertical: 8, borderBottomWidth: 1, borderBottomColor: '#ECE4D8' },
  historyTitle: { color: '#223632', fontSize: 15, fontWeight: '700' },
  historyMeta: { color: '#69756E', fontSize: 13 },
  bullet: { color: '#27443E', fontSize: 15, lineHeight: 22 },
  listRow: { flexDirection: 'row', gap: 10, alignItems: 'flex-start' },
  listDot: { color: '#2E5E52', fontSize: 16, lineHeight: 22, fontWeight: '800' },
  listText: { flex: 1, color: '#40534E', fontSize: 15, lineHeight: 22 },
  tabBar: {
    position: 'absolute',
    left: 16,
    right: 16,
    bottom: 18,
    flexDirection: 'row',
    backgroundColor: '#FFFDF8',
    borderRadius: 24,
    borderWidth: 1,
    borderColor: '#E1D7C8',
    padding: 8,
    gap: 8,
  },
  tabButton: { flex: 1, borderRadius: 18, paddingVertical: 12, alignItems: 'center' },
  tabButtonActive: { backgroundColor: '#183A37' },
  tabButtonText: { color: '#51635D', fontSize: 13, fontWeight: '700' },
  tabButtonTextActive: { color: '#FFFFFF' },
  settingRow: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center', paddingVertical: 8, borderBottomWidth: 1, borderBottomColor: '#E8E0D5' },
  settingLabel: { color: '#3E5B54', fontSize: 15, fontWeight: '600' },
  settingValue: { color: '#183A37', fontSize: 15, fontWeight: '700' },
  sectionOverdue: { backgroundColor: '#FFF5F5', borderRadius: 24, padding: 18, gap: 12, borderWidth: 1, borderColor: '#FFCDD2' },
  sectionUpcoming: { backgroundColor: '#FFF8E1', borderRadius: 24, padding: 18, gap: 12, borderWidth: 1, borderColor: '#FFECB3' },
  sectionTitleOverdue: { color: '#C62828', fontSize: 20, fontWeight: '800' },
  sectionTitleUpcoming: { color: '#F57C00', fontSize: 20, fontWeight: '800' },
  familyMemberCard: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center', paddingVertical: 12, borderBottomWidth: 1, borderBottomColor: '#E8E0D5' },
  familyMemberInfo: { flex: 1, gap: 4 },
  familyMemberName: { color: '#183A37', fontSize: 16, fontWeight: '700' },
  familyMemberRelation: { color: '#5B6C67', fontSize: 13 },
  familyMemberStatus: { flexDirection: 'row', alignItems: 'center', gap: 6 },
  tokenStatusRow: { flexDirection: 'row', alignItems: 'center', gap: 6 },
  tokenStatusDot: { width: 8, height: 8, borderRadius: 4, backgroundColor: '#2E7D32' },
  recipientInfo: { flex: 1, gap: 2 },
  recipientTokenStatus: { color: '#9E9E9E', fontSize: 12, fontStyle: 'italic' },
  statusDot: { width: 8, height: 8, borderRadius: 4 },
  statusDotOnline: { backgroundColor: '#2E7D32' },
  statusDotOffline: { backgroundColor: '#9E9E9E' },
  statusText: { color: '#5B6C67', fontSize: 13 },
  sharedMedCard: { paddingVertical: 10, borderBottomWidth: 1, borderBottomColor: '#E8E0D5' },
  sharedMedName: { color: '#183A37', fontSize: 15, fontWeight: '600' },
  sharedMedDose: { color: '#5B6C67', fontSize: 13 },
  sharingMedCard: { paddingVertical: 12, borderBottomWidth: 1, borderBottomColor: '#E8E0D5', gap: 8 },
  sharingMedInfo: { gap: 2 },
  sharingMedName: { color: '#183A37', fontSize: 15, fontWeight: '700' },
  sharingMedDose: { color: '#5B6C67', fontSize: 13 },
  sharingMedActions: { flexDirection: 'row', gap: 8 },
  sharingSectionLabel: { color: '#3E5B54', fontSize: 13, fontWeight: '600', marginTop: 4 },
  sharingSummary: { color: '#2E7D32', fontSize: 13, fontWeight: '700', marginTop: 4 },
  toggleButton: { paddingHorizontal: 12, paddingVertical: 6, borderRadius: 8 },
  toggleButtonActive: { backgroundColor: '#E8F5E9' },
  toggleButtonInactive: { backgroundColor: '#F5F5F5' },
  toggleButtonText: { fontSize: 12, fontWeight: '700' },
  toggleButtonTextActive: { color: '#2E7D32' },
  toggleButtonTextInactive: { color: '#9E9E9E' },
  memberSelectRow: { flexDirection: 'row', alignItems: 'center', gap: 10, paddingVertical: 6 },
  memberSelectName: { color: '#183A37', fontSize: 14, flex: 1 },
  memberSelectGrid: { flexDirection: 'row', flexWrap: 'wrap', gap: 8 },
  memberSelectChip: { paddingHorizontal: 12, paddingVertical: 8, borderRadius: 999, backgroundColor: '#F5F5F5', borderWidth: 1, borderColor: '#E0E0E0' },
  memberSelectChipActive: { backgroundColor: '#E3F2FD', borderColor: '#1976D2' },
  memberSelectChipText: { color: '#5B6C67', fontSize: 13, fontWeight: '600' },
  memberSelectChipTextActive: { color: '#1976D2' },
  permissionBadge: { backgroundColor: '#E8E0D5', color: '#183A37', fontSize: 11, fontWeight: '700', paddingHorizontal: 8, paddingVertical: 4, borderRadius: 8, overflow: 'hidden' },
  checkbox: { padding: 4 },
  checkboxInner: { width: 20, height: 20, borderRadius: 4, borderWidth: 2, alignItems: 'center', justifyContent: 'center' },
  checkboxChecked: { backgroundColor: '#183A37', borderColor: '#183A37' },
  checkboxUnchecked: { backgroundColor: '#FFFFFF', borderColor: '#C8DBD2' },
  checkmark: { color: '#FFFFFF', fontSize: 12, fontWeight: '800' },
  formGroup: { gap: 8 },
  fieldLabel: { color: '#183A37', fontSize: 14, fontWeight: '700' },
  pickerRow: { flexDirection: 'row', gap: 8 },
  pickerChip: { paddingHorizontal: 14, paddingVertical: 10, borderRadius: 999, backgroundColor: '#F8FCF9', borderWidth: 1, borderColor: '#C8DBD2' },
  pickerChipActive: { backgroundColor: '#183A37', borderColor: '#183A37' },
  pickerChipText: { color: '#284A43', fontSize: 13, fontWeight: '700' },
  pickerChipTextActive: { color: '#FFFFFF' },
  formLabel: { color: '#183A37', fontSize: 14, fontWeight: '700', marginTop: 8 },
  formActions: { flexDirection: 'row', gap: 10, marginTop: 16 },
  buttonDisabled: { opacity: 0.5 },
  reminderCard: { paddingVertical: 12, borderBottomWidth: 1, borderBottomColor: '#E8E0D5', gap: 6 },
  reminderCardNew: { backgroundColor: '#E3F2FD', borderRadius: 8, paddingHorizontal: 8, paddingVertical: 4, marginHorizontal: -4, marginVertical: -4 },
  reminderHeader: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center' },
  reminderMed: { color: '#183A37', fontSize: 15, fontWeight: '700' },
  reminderRecipients: { color: '#5B6C67', fontSize: 13 },
  reminderTime: { color: '#9E9E9E', fontSize: 12 },
  reminderActions: { flexDirection: 'row', gap: 8, marginTop: 8 },
  statusBadge: { paddingHorizontal: 8, paddingVertical: 4, borderRadius: 8 },
  statusBadgeSent: { backgroundColor: '#FFF3E0' },
  statusBadgeDelivered: { backgroundColor: '#E8F5E9' },
  statusBadgeFailed: { backgroundColor: '#FFEBEE' },
  statusBadgeText: { fontSize: 11, fontWeight: '700', textTransform: 'uppercase' },
  familyMemberActions: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center', marginTop: 8 },
  statusToggle: { flexDirection: 'row', alignItems: 'center', gap: 6 },
  statusToggleText: { color: '#5B6C67', fontSize: 13 },
  memberActionButtons: { flexDirection: 'row', gap: 8 },
  iconButton: { paddingHorizontal: 10, paddingVertical: 6, borderRadius: 8, backgroundColor: '#EDF5F1' },
  iconButtonText: { color: '#284A43', fontSize: 12, fontWeight: '600' },
  iconButtonDanger: { paddingHorizontal: 10, paddingVertical: 6, borderRadius: 8, backgroundColor: '#FFEBEE' },
  iconButtonDangerText: { color: '#C62828', fontSize: 12, fontWeight: '600' },
  memberEditForm: { gap: 8 },
  memberEditInput: { backgroundColor: '#F8FCF9', borderRadius: 12, paddingHorizontal: 12, paddingVertical: 10, fontSize: 14, color: '#1F3430', borderWidth: 1, borderColor: '#C8DBD2' },
  memberEditActions: { flexDirection: 'row', gap: 8, justifyContent: 'flex-end' },
  smallButtonPrimary: { paddingHorizontal: 12, paddingVertical: 8, borderRadius: 8, backgroundColor: '#183A37' },
  smallButtonPrimaryText: { color: '#FFFFFF', fontSize: 12, fontWeight: '600' },
  diagnosticsGrid: { flexDirection: 'row', flexWrap: 'wrap', gap: 12, marginTop: 8 },
  diagItem: { minWidth: '45%', flex: 1, backgroundColor: '#F8FCF9', borderRadius: 12, padding: 12, alignItems: 'center', borderWidth: 1, borderColor: '#E8E0D5' },
  diagValue: { color: '#183A37', fontSize: 24, fontWeight: '800' },
  diagLabel: { color: '#5B6C67', fontSize: 12, fontWeight: '600', marginTop: 4 },
  wifiDeviceList: { marginTop: 12, gap: 8 },
  wifiDeviceRow: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center', paddingVertical: 8, paddingHorizontal: 10, backgroundColor: '#F8FCF9', borderRadius: 8, borderWidth: 1, borderColor: '#E8E0D5' },
  wifiDeviceName: { color: '#183A37', fontSize: 14, fontWeight: '600' },
  ocrSection: { backgroundColor: '#FFF8E1', borderRadius: 16, padding: 14, marginBottom: 16, borderWidth: 1, borderColor: '#FFECB3' },
  ocrLabel: { color: '#F57C00', fontSize: 14, fontWeight: '800', marginBottom: 4 },
  ocrHelper: { color: '#5B6C67', fontSize: 13, marginBottom: 8 },
  ocrButtons: { flexDirection: 'row', gap: 8 },
  ocrButton: { flex: 1, backgroundColor: '#FFFDF8', borderRadius: 12, paddingVertical: 12, alignItems: 'center', borderWidth: 1, borderColor: '#FFECB3' },
  ocrButtonText: { color: '#F57C00', fontSize: 14, fontWeight: '700' },
  ocrLoading: { color: '#F57C00', fontSize: 13, fontStyle: 'italic', marginTop: 8 },
  ocrResultBox: { backgroundColor: '#FFFDF8', borderRadius: 12, padding: 12, borderWidth: 1, borderColor: '#FFECB3', gap: 4 },
  ocrResultTitle: { color: '#F57C00', fontSize: 13, fontWeight: '800' },
  ocrResultText: { color: '#3E5B54', fontSize: 13 },
  ocrResultActions: { flexDirection: 'row', gap: 8, marginTop: 8 },
  smallButton: { paddingHorizontal: 12, paddingVertical: 8, borderRadius: 8, backgroundColor: '#EDF5F1' },
  smallButtonText: { color: '#284A43', fontSize: 12, fontWeight: '600' },
  radioOption: { flexDirection: 'row', alignItems: 'center', gap: 10, paddingVertical: 8 },
  radioOuter: { width: 20, height: 20, borderRadius: 10, borderWidth: 2, alignItems: 'center', justifyContent: 'center' },
  radioOuterSelected: { borderColor: '#183A37' },
  radioInner: { width: 10, height: 10, borderRadius: 5, backgroundColor: '#183A37' },
  radioLabel: { color: '#5B6C67', fontSize: 14 },
  radioLabelSelected: { color: '#183A37', fontWeight: '600' },
  checkboxRow: { flexDirection: 'row', alignItems: 'center', gap: 10, paddingVertical: 8 },
  checkboxLabel: { color: '#3E5B54', fontSize: 14 },
  disclaimerText: { color: '#5B6C67', fontSize: 13, lineHeight: 19, marginTop: 8 },
});
