import DataTable from "./DataTable";

export default function Users() {
  return (
    <DataTable
      title="Users"
      path="/api/admin/users"
      fields={[
        { key: "email", label: "Email" },
        { key: "password", label: "Password" },
        { key: "is_admin", label: "Admin", type: "checkbox" },
      ]}
    />
  );
}
