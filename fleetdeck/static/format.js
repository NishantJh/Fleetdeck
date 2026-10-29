"use strict";

// Absolute UI timestamps use the browser's local time zone, independent of its locale.
window.FleetdeckDates = Object.freeze({
  dateTime(value) {
    if (value == null || value === "") return "Unknown date";
    const date = new Date(value);
    if (!Number.isFinite(date.getTime())) return "Unknown date";
    const pad = (n) => String(n).padStart(2, "0");
    return `${String(date.getFullYear()).padStart(4, "0")}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
  },
});
